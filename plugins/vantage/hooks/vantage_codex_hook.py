#!/usr/bin/env python3
"""Send privacy-conscious Codex turn attribution to Vantage as an OTLP log.

This hook reads only structural turn metadata and token totals from Codex
transcripts. It never sends prompts, responses, tool data, transcript paths, or
workspace file contents. Failures never block or continue the Codex turn. It
intentionally uses only the Python standard library.
"""

import argparse
import contextlib
import datetime
import hashlib
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
PLUGIN_VERSION = "0.4.1"
USER_AGENT = f"vantage-codex-plugin/{PLUGIN_VERSION}"
EVENT_NAME = "codex.turn.attribution"
DEFAULT_ENDPOINT = "https://ingest.vantage.sh"
DEFAULT_CLASSIFIER_ENDPOINT = "https://api.vantage.sh/plugins/classifier"
INGEST_ENDPOINT = f"{DEFAULT_ENDPOINT}/v1/logs"
MAX_CLASSIFIER_BYTES = 64 * 1024
CLASSIFIER_TIMEOUT = 2
INGEST_TIMEOUT = 5
SESSION_RETRY_TIMEOUT = 1
TAG_SECONDS = 604_800
TURN_STATE_VERSION = 1
MAX_REGISTERED_TRANSCRIPTS = 50
MAX_OUTBOX_RECORDS = 100
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


def deliver(
    body,
    *,
    token,
    endpoint,
    stderr=sys.stderr,
    timeout=INGEST_TIMEOUT,
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
        response = urllib.request.urlopen(request, timeout=timeout)
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
        if outbox_path(env).is_file():
            retry_outbox(
                env,
                events_path=events_path,
                stderr=stderr,
                timeout=SESSION_RETRY_TIMEOUT,
            )
        return

    token = ingest_token(env)
    if not events_path and not token:
        return

    with turn_state_lock(env):
        bodies = turn_requests(payload, env=env, now_ns=now_ns)
        flush_outbox(
            env,
            bodies,
            events_path=events_path,
            token=token,
            stderr=stderr,
            timeout=INGEST_TIMEOUT,
        )


def retry_outbox(env, *, events_path, stderr, timeout):
    token = "" if events_path else ingest_token(env)
    if not events_path and not token:
        return
    with turn_state_lock(env):
        flush_outbox(
            env,
            load_outbox(env),
            events_path=events_path,
            token=token,
            stderr=stderr,
            timeout=timeout,
        )


def flush_outbox(env, bodies, *, events_path, token, stderr, timeout):
    if not bodies:
        return
    if events_path:
        for body in bodies:
            append_local(body, events_path)
        save_outbox(env, [])
        return
    try:
        response = deliver(
            batch_requests(bodies),
            token=token,
            endpoint=endpoint_for(env),
            stderr=stderr,
            timeout=timeout,
        )
        status = getattr(response, "status", 200)
        if 200 <= status < 300:
            save_outbox(env, [])
            return
    except (OSError, ValueError, urllib.error.URLError):
        pass
    save_outbox(env, bodies)


def batch_requests(bodies):
    resource_logs = []
    for body in bodies:
        if isinstance(body, dict) and isinstance(body.get("resourceLogs"), list):
            resource_logs.extend(body["resourceLogs"])
    return {"resourceLogs": resource_logs}


def turn_requests(payload, env=os.environ, now_ns=None):
    if payload.get("hook_event_name") != "Stop":
        return load_outbox(env)

    session_id = str(payload.get("session_id") or "").strip()
    turn_id = str(payload.get("turn_id") or "").strip()
    if not session_id or not turn_id:
        return load_outbox(env)

    git = workspace_tags(str(payload.get("cwd") or "")) or {}
    tags = skill_tags(env, session_id)
    state = load_turn_state(env)
    changed = update_turn_state(
        state,
        payload,
        now_ns if now_ns is not None else time.time_ns(),
    )
    requests = []
    for summary in changed:
        summary_turn_id = str(summary.get("turn_id") or "").strip()
        if not summary_turn_id:
            continue
        summary_session_id = str(summary.get("session_id") or session_id).strip()
        signature = turn_signature(summary, git, tags)
        if summary.get("emitted_signature") == signature:
            continue
        revision = max(int(summary.get("revision") or 0), 0) + 1
        summary["revision"] = revision
        summary["emitted_signature"] = signature
        attributes = turn_summary_attributes(summary, git, tags)
        requests.append(
            envelope(
                EVENT_NAME,
                summary_session_id,
                git.get("repository"),
                attributes,
                now_ns if now_ns is not None else time.time_ns(),
                "codex_hook",
            )
        )

    queued = deduplicate_requests(load_outbox(env) + requests)
    save_outbox(env, queued)
    save_turn_state(env, state)
    return queued


def transcript_file(payload):
    raw = str(payload.get("transcript_path") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    try:
        return path if path.is_file() else None
    except OSError:
        return None


def update_turn_state(state, payload, stopped_at_ns):
    session_id = str(payload.get("session_id") or "").strip()
    current_turn_id = str(payload.get("turn_id") or "").strip()
    path = transcript_file(payload)
    changed_ids = set()
    current_start_ns = stopped_at_ns

    if path is not None:
        transcript_key = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
        transcripts = state.setdefault("transcripts", {})
        transcript = transcripts.setdefault(
            transcript_key,
            {"path": str(path.resolve()), "offset": 0, "session_id": session_id},
        )
        transcript["path"] = str(path.resolve())
        transcript["session_id"] = session_id
        offset = max(int(transcript.get("offset") or 0), 0)
        try:
            size = path.stat().st_size
            if size < offset:
                offset = 0
            with path.open("rb") as handle:
                handle.seek(offset)
                while True:
                    line_start = handle.tell()
                    line = handle.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        handle.seek(line_start)
                        break
                    offset = handle.tell()
                    try:
                        record = json.loads(line.decode("utf-8"))
                    except (UnicodeError, json.JSONDecodeError):
                        continue
                    changed_ids.update(apply_transcript_record(state, session_id, record))
            transcript["offset"] = offset
            transcript["seen_at_ns"] = stopped_at_ns
        except OSError:
            pass
        prune_transcripts(transcripts)

    turns = state.setdefault("turns", {})
    current_key = turn_key(session_id, current_turn_id)
    current = turns.setdefault(
        current_key,
        new_turn_summary(session_id, current_turn_id, stopped_at_ns),
    )
    if path is None:
        current["last_seen_ns"] = max(
            int(current.get("last_seen_ns") or 0), stopped_at_ns
        )
    current_start_ns = int(current.get("started_at_ns") or stopped_at_ns)
    changed_ids.add(current_key)

    for key, summary in turns.items():
        if key == current_key or summary.get("state") == "completed":
            continue
        if str(summary.get("session_id") or "") != session_id:
            continue
        last_seen_ns = int(summary.get("last_seen_ns") or 0)
        if last_seen_ns and last_seen_ns < current_start_ns:
            summary["state"] = "interrupted"
            summary["ended_at_ns"] = last_seen_ns
            summary["recovered"] = True
            changed_ids.add(key)

    changed = [turns[key] for key in changed_ids if key in turns]
    changed.sort(key=lambda value: (int(value.get("started_at_ns") or 0), value.get("turn_id", "")))
    return changed


def apply_transcript_record(state, session_id, record):
    if not isinstance(record, dict):
        return set()
    record_type = record.get("type")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return set()
    timestamp_ns = parse_timestamp_ns(record.get("timestamp"))
    if timestamp_ns is None:
        return set()

    root_turn_id = str(payload.get("root_turn_id") or payload.get("turn_id") or "").strip()
    if not root_turn_id:
        attribution = payload.get("turn_attribution")
        if isinstance(attribution, dict):
            root_turn_id = str(
                attribution.get("root_turn_id") or attribution.get("turn_id") or ""
            ).strip()
    if not root_turn_id:
        return set()

    key = turn_key(session_id, root_turn_id)
    turns = state.setdefault("turns", {})
    summary = turns.setdefault(
        key,
        new_turn_summary(session_id, root_turn_id, timestamp_ns),
    )
    summary["started_at_ns"] = min(
        int(summary.get("started_at_ns") or timestamp_ns), timestamp_ns
    )
    summary["last_seen_ns"] = max(
        int(summary.get("last_seen_ns") or 0), timestamp_ns
    )

    event_type = str(payload.get("type") or "")
    event_turn_id = str(payload.get("turn_id") or "").strip()
    if record_type == "turn_context" and event_turn_id == root_turn_id:
        model = str(payload.get("model") or "").strip()
        if model:
            summary["model"] = model
    elif (
        record_type == "event_msg"
        and event_type == "task_started"
        and event_turn_id == root_turn_id
    ):
        summary["started_at_ns"] = timestamp_ns
    elif (
        record_type == "event_msg"
        and event_type == "task_complete"
        and event_turn_id == root_turn_id
    ):
        summary["state"] = "completed"
        summary["ended_at_ns"] = timestamp_ns
    elif record_type == "token_usage_record":
        leaf_turn_id = str(payload.get("turn_id") or root_turn_id).strip()
        usage = payload.get("turn_token_usage")
        if isinstance(usage, dict):
            leaves = summary.setdefault("leaf_usage", {})
            previous = leaves.get(leaf_turn_id)
            if not isinstance(previous, dict) or timestamp_ns >= int(previous.get("at_ns") or 0):
                leaves[leaf_turn_id] = {
                    "at_ns": timestamp_ns,
                    "input_tokens": nonnegative_int(usage.get("input_tokens")),
                    "cached_input_tokens": nonnegative_int(usage.get("cached_input_tokens")),
                    "cache_write_input_tokens": nonnegative_int(usage.get("cache_write_input_tokens")),
                    "output_tokens": nonnegative_int(usage.get("output_tokens")),
                    "reasoning_output_tokens": nonnegative_int(usage.get("reasoning_output_tokens")),
                    "total_tokens": nonnegative_int(usage.get("total_tokens")),
                }
        response_id = str(payload.get("response_id") or "").strip()
        if response_id:
            responses = summary.setdefault("response_ids", [])
            if response_id not in responses:
                responses.append(response_id)
    return {key}


def new_turn_summary(session_id, turn_id, timestamp_ns):
    return {
        "session_id": session_id,
        "turn_id": turn_id,
        "started_at_ns": timestamp_ns,
        "last_seen_ns": timestamp_ns,
        "ended_at_ns": 0,
        "state": "partial",
        "recovered": False,
        "model": "",
        "leaf_usage": {},
        "response_ids": [],
        "revision": 0,
    }


def turn_summary_attributes(summary, git, tags):
    usage = turn_usage(summary)
    session_id = str(summary.get("session_id") or "")
    turn_id = str(summary.get("turn_id") or "")
    values = [
        string_attribute("codex.turn.id", turn_id),
        string_attribute("vantage.turn.logical_id", turn_logical_id(session_id, turn_id)),
        integer_attribute("vantage.turn.revision", summary.get("revision")),
        string_attribute("vantage.turn.state", summary.get("state")),
        boolean_attribute("vantage.turn.recovered", bool(summary.get("recovered"))),
        integer_attribute("codex.turn.started_at_unix_nano", summary.get("started_at_ns")),
        integer_attribute("codex.turn.ended_at_unix_nano", summary.get("ended_at_ns")),
        integer_attribute("codex.turn.request_count", len(summary.get("response_ids") or [])),
        integer_attribute("codex.turn.input_token_count", usage["input_tokens"]),
        integer_attribute("codex.turn.cached_input_token_count", usage["cached_input_tokens"]),
        integer_attribute("codex.turn.cache_write_input_token_count", usage["cache_write_input_tokens"]),
        integer_attribute("codex.turn.output_token_count", usage["output_tokens"]),
        integer_attribute("codex.turn.reasoning_output_token_count", usage["reasoning_output_tokens"]),
        integer_attribute("codex.turn.total_token_count", usage["total_tokens"]),
        string_attribute("model", summary.get("model")),
        string_attribute("vcs.ref.head.name", git.get("branch")),
    ]
    return compact(values) + classification_attributes(tags)


def turn_usage(summary):
    totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": 0,
    }
    leaves = summary.get("leaf_usage")
    if not isinstance(leaves, dict):
        return totals
    for usage in leaves.values():
        if not isinstance(usage, dict):
            continue
        for key in totals:
            totals[key] += nonnegative_int(usage.get(key))
    return totals


def turn_signature(summary, git, tags):
    value = {
        "turn_id": summary.get("turn_id"),
        "started_at_ns": summary.get("started_at_ns"),
        "ended_at_ns": summary.get("ended_at_ns"),
        "state": summary.get("state"),
        "recovered": bool(summary.get("recovered")),
        "model": summary.get("model"),
        "request_count": len(summary.get("response_ids") or []),
        "usage": turn_usage(summary),
        "repository": git.get("repository"),
        "branch": git.get("branch"),
        "tags": tags,
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def turn_logical_id(session_id, turn_id):
    value = f"codex:{session_id}:{turn_id}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def turn_key(session_id, turn_id):
    return f"{session_id}:{turn_id}"


def parse_timestamp_ns(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(parsed.timestamp() * 1_000_000_000)
    except (ValueError, OverflowError):
        return None


def nonnegative_int(value):
    try:
        return max(int(value), 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def turn_state_path(env):
    configured = str(env.get("VANTAGE_CODEX_HOOK_STATE_DIR", "")).strip()
    directory = Path(configured) if configured else classifier_directory(env)
    return directory / "turn_state.json"


def outbox_path(env):
    return turn_state_path(env).with_name("turn_outbox.json")


@contextlib.contextmanager
def turn_state_lock(env):
    path = turn_state_path(env).with_name("turn_state.lock")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a+") as handle:
        lock_file(handle)
        yield


def load_turn_state(env):
    try:
        value = json.loads(turn_state_path(env).read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("version") == TURN_STATE_VERSION:
            value.setdefault("transcripts", {})
            value.setdefault("turns", {})
            return value
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return {"version": TURN_STATE_VERSION, "transcripts": {}, "turns": {}}


def save_turn_state(env, state):
    prune_turns(state.setdefault("turns", {}))
    write_private_file(
        turn_state_path(env),
        json.dumps(state, separators=(",", ":"), sort_keys=True).encode("utf-8"),
    )


def load_outbox(env):
    try:
        value = json.loads(outbox_path(env).read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []


def save_outbox(env, bodies):
    bodies = deduplicate_requests(bodies)[-MAX_OUTBOX_RECORDS:]
    if not bodies:
        try:
            outbox_path(env).unlink()
        except FileNotFoundError:
            pass
        return
    write_private_file(
        outbox_path(env),
        json.dumps(bodies, separators=(",", ":")).encode("utf-8"),
    )


def deduplicate_requests(bodies):
    seen = set()
    output = []
    for body in bodies:
        event_id = request_attribute(body, "vantage.event_id")
        if not event_id or event_id in seen:
            continue
        seen.add(event_id)
        output.append(body)
    return output


def request_attribute(body, key):
    try:
        attributes = body["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["attributes"]
        for attribute in attributes:
            if attribute.get("key") == key:
                value = attribute.get("value")
                if isinstance(value, dict):
                    return str(value.get("stringValue") or "")
    except (KeyError, IndexError, TypeError):
        pass
    return ""


def prune_transcripts(transcripts):
    while len(transcripts) > MAX_REGISTERED_TRANSCRIPTS:
        oldest = min(
            transcripts,
            key=lambda key: int(transcripts[key].get("seen_at_ns") or 0),
        )
        del transcripts[oldest]


def prune_turns(turns):
    maximum = MAX_REGISTERED_TRANSCRIPTS * 50
    while len(turns) > maximum:
        oldest = min(turns, key=lambda key: int(turns[key].get("last_seen_ns") or 0))
        del turns[oldest]


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


def integer_attribute(key, value):
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number < 0:
        return None
    return {"key": key, "value": {"intValue": str(number)}}


def boolean_attribute(key, value):
    return {"key": key, "value": {"boolValue": bool(value)}}


def compact(values):
    return [value for value in values if value is not None]


if __name__ == "__main__":
    if sys.argv[1:2] == ["tag"]:
        raise SystemExit(tag(sys.argv[2:]))
    run()
