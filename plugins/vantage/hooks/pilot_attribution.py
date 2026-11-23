#!/usr/bin/env python3
"""Vantage staging attribution hook. Run directly with Python 3.9+ on POSIX.
SQLite captures evidence. A detached copy of this script exports OTLP/gRPC.
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import os
from functools import lru_cache
import re
import random
import platform
import shlex
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

COMMON = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "SessionEnd",
)


@dataclass(frozen=True)
class Agent:
    turn_field: str
    events: tuple[str, ...]
    child_owner_field: str | None = None

    def session(self, payload):
        return identifier(
            payload.get(self.child_owner_field) or payload.get("session_id")
        )


AGENTS = {
    "codex": Agent(
        "turn_id", COMMON + ("PostModelRequest", "SubagentStart"), "agent_id"
    ),
    "claude_code": Agent("prompt_id", COMMON),
}


def identifier(value):
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[\w.:/-]{1,180}", value, re.ASCII)
        else None
    )


def git_email(cwd):
    """Read the effective config in the agent's worktree; never inspect commit authors."""
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return None  # No observation. Keep any earlier observation for this turn.
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "config", "--get", "user.email"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return None
    email = result.stdout.rstrip("\r\n")
    if (
        result.returncode == 0
        and len(email) <= 254
        and re.fullmatch(r"[^@\s\x00-\x1f\x7f]+@[^@\s\x00-\x1f\x7f]+", email)
    ):
        return email
    return ""  # Missing/invalid config clears a previous observation.


def git_facts(cwd, salt):
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return {}

    def git(*args):
        p = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return p.stdout.strip() if p.returncode == 0 else ""

    try:
        worktree = git("rev-parse", "--absolute-git-dir")
        if not worktree:
            return {}
        facts = {
            "worktree": hashlib.sha256(salt + os.fsencode(worktree)).hexdigest(),
            "branch": git("symbolic-ref", "--short", "-q", "HEAD"),
            "head": git("rev-parse", "--verify", "HEAD"),
        }
        default_ref = git("symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD")
        if default_ref.startswith("origin/") and facts["branch"]:
            facts["branch_is_default"] = facts["branch"] == default_ref.removeprefix(
                "origin/"
            )
        remote = git("remote", "get-url", "origin")
        if "://" not in remote:
            remote = re.sub(r"^([\w.-]+@)?([\w.-]+):", r"ssh://\2/", remote)
        url = urlsplit(remote)
        if url.hostname and url.scheme in {"https", "http", "ssh", "git"}:
            repo = url.path.strip("/").removesuffix(".git")
            parts = repo.split("/")
            if len(parts) >= 2 and re.fullmatch(r"[\w./-]+", repo) and not any(part in {"", ".", ".."} for part in parts):
                facts["repository"] = f"{url.hostname}/{repo}"
        return facts
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


REQUEST = "vantage.request_correlation"
TURN = "vantage.turn_attribution"
HOME = Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser().resolve()
PROFILE = HOME / "vantage" / "staging-attribution.json"
ENDPOINT = "https://ingest-staging.vantage.sh"
STATE = None
TOKEN = None
OPT_IN = None
SOURCE = "tag"


def load_profile():
    global STATE, TOKEN, OPT_IN
    profile = json.loads(PROFILE.read_text())
    TOKEN = profile["token"]
    if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{8,512}", TOKEN):
        raise ValueError("invalid staging credential")
    binding = hashlib.sha256(TOKEN.encode()).hexdigest()
    STATE = PROFILE.parent / "outbox" / binding
    OPT_IN = profile["since_ns"]


def connect():
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(STATE / "state.sqlite", timeout=5, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            agent TEXT, session TEXT, home TEXT, offset INTEGER DEFAULT 0,
            PRIMARY KEY(agent, session));
        CREATE TABLE IF NOT EXISTS turns (
            agent TEXT, session TEXT, turn TEXT, revision INTEGER, state TEXT,
            PRIMARY KEY(agent, session, turn));
        CREATE TABLE IF NOT EXISTS outbox (
            id TEXT PRIMARY KEY, timestamp INTEGER, payload TEXT);
        CREATE TABLE IF NOT EXISTS git_identity (
            agent TEXT, session TEXT, turn TEXT, at_ns INTEGER, email TEXT NOT NULL,
            PRIMARY KEY(agent, session, turn, at_ns));
        CREATE TABLE IF NOT EXISTS install (id INTEGER PRIMARY KEY, salt BLOB NOT NULL);
        INSERT OR IGNORE INTO install VALUES (1,randomblob(16));
        CREATE TABLE IF NOT EXISTS diagnostics (name TEXT PRIMARY KEY, count INTEGER);
        CREATE TABLE IF NOT EXISTS cli_versions (agent TEXT, session TEXT, version TEXT,
            PRIMARY KEY(agent, session));
        CREATE TABLE IF NOT EXISTS turn_models (agent TEXT, session TEXT, turn TEXT, model TEXT,
            PRIMARY KEY(agent, session, turn, model));
        CREATE TABLE IF NOT EXISTS sender (id INTEGER PRIMARY KEY, failures INTEGER DEFAULT 0,
            retry_at REAL DEFAULT 0, last_ack REAL DEFAULT 0, discovered REAL DEFAULT 0);
        INSERT OR IGNORE INTO sender(id) VALUES(1);
        CREATE TABLE IF NOT EXISTS recovery_queue (session TEXT PRIMARY KEY, generation INTEGER);
        CREATE TABLE IF NOT EXISTS rollout_paths (session TEXT PRIMARY KEY, path TEXT);
        CREATE TABLE IF NOT EXISTS versions_at (agent TEXT, session TEXT, at_ns INTEGER, version TEXT,
            PRIMARY KEY(agent,session,at_ns));
    """)
    return db


@contextmanager
def write(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def observed_email(db, key, at_ns):
    row = db.execute(
        "SELECT email FROM git_identity WHERE agent=? AND session=? AND turn=? "
        "AND at_ns<=? ORDER BY at_ns DESC LIMIT 1",
        (*key, at_ns),
    ).fetchone()
    return row[0] if row else ""


@lru_cache(maxsize=1)
def device_facts():
    return {"os.type": platform.system().lower(), "os.version": platform.release(),
            "host.arch": platform.machine()}


def enqueue(db, kind, key, attrs, timestamp=None, event_id=None):
    agent, session, turn = key
    event_id = event_id or str(uuid.uuid4())
    timestamp = timestamp if timestamp is not None else time.time_ns()
    email = observed_email(db, key, timestamp)
    version = db.execute("SELECT version FROM versions_at WHERE agent=? AND session=? AND at_ns<=? "
                         "ORDER BY at_ns DESC LIMIT 1", (*key[:2], timestamp)).fetchone()
    models = [row[0] for row in db.execute("SELECT model FROM turn_models WHERE agent=? AND session=? AND turn=?", key)]
    turn_model = models[0] if len(models) == 1 else "multiple" if models else "unknown"
    summary = kind == TURN
    payload = {
        "git.identity.source": "git_config" if email else "unknown",
        "agent.version": version[0] if version else "unknown",
        "model.source": ("turn" if models else "unknown") if summary else ("request" if attrs.get("model") else "unknown"),
        "model": turn_model if summary else "unknown",
        **({key: "unknown" for key in ("os.type", "os.version", "host.arch")}
           if SOURCE == "rollout" else device_facts()),
        "vantage.capture.source": SOURCE,
        "vantage.plugin.version": "0.5.0",
        "vantage.schema.version": "1",
        "event.name": kind,
        "event_id": event_id,
        "agent": agent,
        "session.id": session,
        "turn.id": turn,
        **attrs,
    }
    if email:
        payload["git.user.email"] = email
    db.execute(
        "INSERT OR IGNORE INTO outbox VALUES (?,?,?)",
        (event_id, timestamp, json.dumps(payload)),
    )


def snapshot(
    db, key, *, facts=None, changes=None, tags=None, tag_source="manual", timestamp=None
):
    row = db.execute(
        "SELECT revision,state FROM turns WHERE agent=? AND session=? AND turn=?", key
    ).fetchone()
    revision, state = (
        (row[0], json.loads(row[1])) if row else (0, {"tags": {}, "git": []})
    )
    previous = json.dumps(state, sort_keys=True)
    if facts and (not state["git"] or state["git"][-1]["facts"] != facts):
        state["git"].append({"at_ns": time.time_ns(), "facts": facts})
    for k, v in (changes or {}).items():
        if (
            k in ("first_seen_ns", "first_source", "started_ns", "ended_ns")
            and k in state
        ):
            continue
        state[k] = v
    for name, value in (tags or {}).items():
        if value is None:
            state["tags"].pop(name, None)
            state.setdefault("tag_sources", {}).pop(name, None)
        else:
            state["tags"][name] = value
            state.setdefault("tag_sources", {})[name] = tag_source
    encoded = json.dumps(state, sort_keys=True)
    while len(encoded.encode()) > 64 * 1024 and len(state["git"]) > 2:
        del state["git"][1]
        state["omitted_git_observations"] = state.get("omitted_git_observations", 0) + 1
        encoded = json.dumps(state, sort_keys=True)
    if row and previous == encoded:
        return
    db.execute(
        "INSERT OR REPLACE INTO turns VALUES (?,?,?,?,?)", (*key, revision + 1, encoded)
    )
    enqueue(
        db, TURN, key, {"revision": revision + 1, "state": encoded}, timestamp=timestamp
    )


def correlation(
    db, key, usage, source, *, status, model=None, response=None, **options
):
    attrs = {"source": source, "status": status}
    if model:
        attrs["model"] = model
    if response:
        attrs["response_id"] = response
    for name in (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    ):
        value = (usage or {}).get(name)
        if type(value) is int and 0 <= value < 2**63:
            attrs["fp." + name] = value
    enqueue(db, REQUEST, key, attrs, **options)


def project(db, session, offset, line):
    entry = json.loads(line)
    if entry.get("type") == "session_meta":
        meta = entry.get("payload", {})
        version = identifier(meta.get("cli_version"))
        if version and meta.get("id") == session:
            when = datetime.datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
            db.execute("INSERT OR REPLACE INTO versions_at VALUES ('codex',?,?,?)",
                       (session, int(when.timestamp() * 1e9), version))
        return
    if entry.get("type") != "token_usage_record":
        return
    p = entry["payload"]
    turn = identifier(p.get("turn_id"))
    if p.get("thread_id") != session or not turn:
        return
    when = datetime.datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))  # noqa: FURB162 -- Python 3.9
    key = ("codex", session, turn)
    ns = int(when.timestamp() * 1e9)
    if OPT_IN and ns < OPT_IN:
        return
    response = identifier(p.get("response_id"))
    event_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"codex:{session}:{response or offset}")
    )
    snapshot(
        db, key, changes={"first_seen_ns": ns, "first_source": "rollout"}, timestamp=ns
    )
    correlation(
        db,
        key,
        p["usage"],
        "rollout",
        status="completed",
        response=response,
        model=identifier(p.get("model")),
        timestamp=ns,
        event_id=event_id,
    )


def recover(db, targets=None, *, after_chunk=None):
    sessions = db.execute("SELECT session,home,offset FROM sessions WHERE agent='codex'")
    by_home = {str(HOME): {}} if OPT_IN else {}
    for session, home, offset in sessions:
        by_home.setdefault(home, {})[session] = offset
    for home, offsets in by_home.items():
        if targets is None:
            paths = (path for directory in ("sessions", "archived_sessions")
                     for path in (Path(home) / directory).rglob("*.jsonl"))
        else:
            paths = [Path(row[0]) for session in targets if session in offsets
                     for row in db.execute("SELECT path FROM rollout_paths WHERE session=?", (session,))]
        for path in paths:
            # Codex file names end with the 36-character owning thread UUID.
            session = path.stem[-36:]
            if session not in offsets:
                # Dedicated pilot home: only discover threads created after opt-in.
                # A UUIDv7 timestamp is independent of file copy or archive times.
                try:
                    thread = uuid.UUID(session)
                except ValueError:
                    continue
                if not OPT_IN or thread.version != 7 or (thread.int >> 80) * 1_000_000 < OPT_IN:
                    continue
                db.execute("INSERT OR IGNORE INTO sessions(agent,session,home) VALUES ('codex',?,?)", (session, home))
                offsets[session] = 0
            offset = offsets[session]
            try:
                with path.open("rb") as stream:
                    stream.seek(offset if path.stat().st_size >= offset else 0)
                    while True:
                        lines = []
                        for _ in range(128):
                            start = stream.tell()
                            line = stream.readline()
                            if not line.endswith(b"\n"):
                                stream.seek(start)
                                break
                            lines.append((start, line))
                        if not lines:
                            break
                        with write(db):
                            for start, line in lines:
                                try:
                                    project(db, session, start, line)
                                except (
                                    ValueError,
                                    KeyError,
                                    TypeError,
                                    AttributeError,
                                    OverflowError,
                                ):
                                    # Count malformed completed lines without recording their contents.
                                    db.execute(
                                        "INSERT INTO diagnostics VALUES ('invalid_rollout',1) ON CONFLICT(name) DO UPDATE SET count=count+1"
                                    )
                            db.execute(
                                "UPDATE sessions SET offset=? WHERE agent='codex' AND session=?",
                                (stream.tell(), session),
                            )
                        offsets[session] = stream.tell()
                        if after_chunk:
                            after_chunk(db)
            except OSError:
                db.execute("INSERT INTO diagnostics VALUES ('rollout_io_errors',1) "
                           "ON CONFLICT(name) DO UPDATE SET count=count+1")
                continue
            db.execute("INSERT OR REPLACE INTO rollout_paths VALUES (?,?)", (session, str(path)))


# The sender only produces OTLP logs with scalar attributes. curl owns the
# HTTP/2 session and verifies transport errors. All protobuf field numbers
# below come from opentelemetry-proto's logs/v1 and common/v1 schemas.
def varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def message(number, payload):
    return varint(number << 3 | 2) + varint(len(payload)) + payload


def string(number, value):
    return message(number, str(value).encode("utf-8"))


def attribute(key, value):
    if type(value) is int:
        encoded = b"\x18" + varint(value & ((1 << 64) - 1))  # AnyValue.int_value
    else:
        encoded = string(1, value)
    return string(1, key) + message(2, encoded)


def encode_logs(service, records):
    resource = message(1, attribute("service.name", service))
    scope_logs = bytearray(message(1, string(1, service)))
    for timestamp, attrs in records:
        log = bytearray(
            b"\x09" + struct.pack("<Q", timestamp)
        )  # fixed64 time_unix_nano
        log.extend(message(5, string(1, attrs["event.name"])))
        for key, value in attrs.items():
            log.extend(message(6, attribute(key, value)))
        scope_logs.extend(message(2, log))
    return message(1, message(1, resource) + message(2, scope_logs))


def require_ack(headers, frame):
    # gRPC can return an HTTP 200 for a failed RPC. Also require the final
    # grpc-status and an empty ExportLogsServiceResponse (possibly containing
    # an empty partial_success submessage, as emitted by the Collector).
    fields = headers.lower().splitlines()
    # HTTPS proxies may prepend their CONNECT response.
    fields = fields[next((i for i in reversed(range(len(fields))) if fields[i].startswith(b"http/")), 0):]
    statuses = [
        line.partition(b":")[2].strip()
        for line in fields
        if line.startswith(b"grpc-status:")
    ]
    if (
        not fields
        or not re.fullmatch(rb"http/2 200\s*", fields[0])
        or statuses != [b"0"]
    ):
        raise RuntimeError("collector did not acknowledge the gRPC export")
    if frame not in (b"\x00\x00\x00\x00\x00", b"\x00\x00\x00\x00\x02\x0a\x00"):
        raise RuntimeError(
            "collector returned a partial or unrecognized response; batch retained"
        )


def export(service, records, *, token=None):
    body = encode_logs(service, records)
    frame = b"\x00" + struct.pack(">I", len(body)) + body
    url = f"{ENDPOINT}/opentelemetry.proto.collector.logs.v1.LogsService/Export"
    # Keep binary data on stdin/stdout. dump-header includes HTTP/2 trailers.
    with tempfile.TemporaryFile() as headers, tempfile.TemporaryFile() as auth:
        auth.write(f"authorization: Bearer {token or TOKEN}\n".encode())
        auth.seek(0)
        args = [
            "curl",
            "--disable",
            "--silent",
            "--show-error",
            "--fail",
            "--http2",
            "--header",
            f"@/dev/fd/{auth.fileno()}",
            "--max-time",
            "5",
            "--header",
            "content-type: application/grpc",
            "--header",
            "te: trailers",
            "--dump-header",
            f"/dev/fd/{headers.fileno()}",
            "--data-binary",
            "@-",
            url,
        ]
        reply = subprocess.run(
            args,
            input=frame,
            stdout=subprocess.PIPE,
            check=True,
            pass_fds=(headers.fileno(), auth.fileno()),
            timeout=7,
        )
        headers.seek(0)
        require_ack(headers.read(), reply.stdout)


def drain(db, *, single=False):
    while rows := db.execute(
        "SELECT id,timestamp,payload FROM outbox ORDER BY rowid LIMIT 64"
    ).fetchall():
        export("vantage-agent-hook", [(ts, json.loads(data)) for _, ts, data in rows])
        with write(db):
            db.executemany("DELETE FROM outbox WHERE id=?", [(row[0],) for row in rows])
            db.execute("UPDATE sender SET failures=0,retry_at=0,last_ack=? WHERE id=1", (time.time(),))
        if single:
            return


def schedule_recovery(db, session):
    db.execute("INSERT INTO recovery_queue VALUES (?,1) ON CONFLICT(session) "
               "DO UPDATE SET generation=generation+1", (session,))


def send_batch_if_due(db):
    failures, retry_at = db.execute("SELECT failures,retry_at FROM sender WHERE id=1").fetchone()
    if retry_at > time.time():
        return
    try:
        drain(db, single=True)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        delay = min(300, 5 * 2 ** min(failures, 6) * random.uniform(0.8, 1.2))
        db.execute("UPDATE sender SET failures=failures+1,retry_at=? WHERE id=1",
                   (time.time() + delay,))


def run_sender(db, lock):
    while True:
        requests = db.execute("SELECT session,generation FROM recovery_queue").fetchall()
        send_batch_if_due(db)
        if requests:
            targets = {session for session, _ in requests}
            missing_path = db.execute("SELECT 1 FROM recovery_queue q LEFT JOIN rollout_paths p "
                                      "USING(session) WHERE p.path IS NULL LIMIT 1").fetchone()
            discovered = db.execute("SELECT discovered FROM sender").fetchone()[0]
            discover = bool(missing_path) or time.time() - discovered >= 300
            recover(db, None if discover else targets, after_chunk=send_batch_if_due)
            with write(db):
                if discover:
                    db.execute("UPDATE sender SET discovered=? WHERE id=1", (time.time(),))
                db.executemany("DELETE FROM recovery_queue WHERE session=? AND generation=?", requests)
        # Serialize the idle decision with hook writes. A writer that commits
        # after this check will find the lock available when it kicks the sender.
        with write(db):
            pending = db.execute("SELECT EXISTS(SELECT 1 FROM recovery_queue) OR "
                                 "(retry_at<=? AND EXISTS(SELECT 1 FROM outbox)) FROM sender",
                                 (time.time(),)).fetchone()[0]
            if not pending:
                fcntl.flock(lock, fcntl.LOCK_UN)
                return


def launch(db, recover_files=False, *, foreground=False, force=False):
    if recover_files:
        schedule_recovery(db, "*")
    if force and foreground:
        db.execute("UPDATE sender SET retry_at=0 WHERE id=1")
    runnable = db.execute("SELECT EXISTS(SELECT 1 FROM recovery_queue) OR "
                          "(retry_at<=? AND EXISTS(SELECT 1 FROM outbox)) FROM sender",
                          (time.time(),)).fetchone()[0]
    if not runnable:
        return
    with (STATE / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        if foreground:
            run_sender(db, lock)
            return
        subprocess.Popen(
            [sys.executable, "-I", str(Path(__file__).resolve()), "send",
             "--binding", STATE.name, "--lock-fd", str(lock.fileno())],
            pass_fds=(lock.fileno(),), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def installed_version():
    try:
        result = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            return identifier(result.stdout.strip().removeprefix("codex-cli "))
    except (OSError, UnicodeError, subprocess.SubprocessError):
        pass
    return None


def handle(db, name, payload):
    global SOURCE
    agent = AGENTS[name]
    event = payload.get("hook_event_name")
    session = agent.session(payload)
    turn = identifier(payload.get(agent.turn_field))
    if not session or event not in agent.events:
        return
    SOURCE = event
    model = identifier(payload.get("model"))
    version = identifier(payload.get("cli_version") or payload.get("agent_version"))
    if not version and name == "codex" and (event == "SessionStart" or not db.execute(
        "SELECT 1 FROM cli_versions WHERE agent=? AND session=?", (name, session)).fetchone()):
        version = installed_version() or "unknown"
    key = (name, session, turn)
    now = time.time_ns()
    home = str(Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser().resolve())
    tool = event in ("PreToolUse", "PostToolUse")
    if tool and payload.get("tool_name") not in {
        "Bash",
        "Edit",
        "Write",
        "MultiEdit",
        "exec_command",
        "apply_patch",
        "shell",
        "shell_command",
    }:
        return
    first = (
        turn
        and not db.execute(
            "SELECT 1 FROM turns WHERE agent=? AND session=? AND turn=?", key
        ).fetchone()
    )
    observe = turn and (
        first or tool or event in {"UserPromptSubmit", "Stop", "SessionEnd"}
    )
    email = git_email(payload.get("cwd")) if turn else None
    facts = None
    if observe:
        salt = db.execute("SELECT salt FROM install WHERE id=1").fetchone()[0]
        facts = git_facts(payload.get("cwd"), salt)
    with write(db):
        db.execute(
            "INSERT OR IGNORE INTO sessions(agent,session,home) VALUES (?,?,?)",
            (name, session, home),
        )
        if version:
            db.execute("INSERT OR REPLACE INTO versions_at VALUES (?,?,?,?)", (name, session, now, version))
            db.execute("INSERT OR REPLACE INTO cli_versions VALUES (?,?,?)", (name, session, version))
        if turn:
            changes = {"first_seen_ns": now, "first_source": event}
            if model:
                db.execute("INSERT OR IGNORE INTO turn_models VALUES (?,?,?,?)", (*key, model))
                changes["models"] = [r[0] for r in db.execute(
                    "SELECT model FROM turn_models WHERE agent=? AND session=? AND turn=? ORDER BY model", key)]
            if email is not None and email != observed_email(db, key, now):
                db.execute(
                    "INSERT OR REPLACE INTO git_identity VALUES (?,?,?,?,?)",
                    (*key, now, email),
                )
                changes["identity_observed_ns"] = now
            if event == "UserPromptSubmit":
                changes["started_ns"] = now
            if event in ("Stop", "SessionEnd"):
                changes["ended_ns"] = now
            if agent.child_owner_field and session != payload.get("session_id"):
                parent = identifier(payload.get("session_id"))
                if parent:
                    changes["parent_session"] = parent
            snapshot(
                db,
                key,
                facts=facts,
                changes=changes,
                timestamp=now,
            )
            if event == "PostModelRequest":
                correlation(
                    db,
                    key,
                    payload.get("token_usage"),
                    "model_hook",
                    timestamp=now,
                    status=payload.get("status", "unknown"),
                    model=identifier(payload.get("model")),
                )
        if event in {"SessionStart", "Stop", "SessionEnd"}:
            schedule_recovery(db, "*" if event == "SessionStart" else session)
    if turn and event == "UserPromptSubmit":
        command = shlex.join(
            [
                "env",
                "CODEX_HOME=" + str(HOME),
                sys.executable,
                "-I",
                str(HOME / "vantage/hooks/run_vantage_codex_hook.py"),
                "tag",
                name,
                session,
                turn,
            ]
        )
        message = f"When PR, ticket, or purpose is known, use {command} --set name=value --source inferred for this turn."
        classifier = Path(
            os.environ.get(
                "VANTAGE_CLASSIFIER_FILE", Path(home) / "vantage/classifier.md"
            )
        ).expanduser()
        if classifier.is_file():
            message = (
                f"Read the Vantage classifier at {shlex.quote(str(classifier))}. "
                f"Apply its dimensions to this turn with {command} --source inferred "
                "--tag key=value (repeat for additional dimensions)."
            )
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": event,
                        "additionalContext": message,
                    }
                }
            )
        )
    launch(db)


def main():
    global SOURCE
    os.umask(0o077)
    load_profile()
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("hook").add_argument("agent", choices=AGENTS)
    tag = sub.add_parser("tag")
    tag.add_argument("agent", choices=AGENTS)
    tag.add_argument("session")
    tag.add_argument("turn")
    tag.add_argument(
        "--source", choices=["manual", "inferred", "explicit"], default="manual"
    )
    group = tag.add_mutually_exclusive_group(required=True)
    group.add_argument("--set", "--tag", action="append", metavar="NAME=VALUE")
    group.add_argument("--clear", metavar="NAME")
    for action in ("recover", "flush", "status"):
        sub.add_parser(action).add_argument("--binding")
    sender = sub.add_parser("send")
    sender.add_argument("--lock-fd", type=int, required=True)
    sender.add_argument("--binding", required=True)
    args = parser.parse_args(sys.argv[1:] or ["hook", "codex"])
    if getattr(args, "binding", None) and args.binding != STATE.name:
        return
    if args.action == "hook":
        # Coding clients must be able to continue even if capture is unavailable.
        try:
            db = connect()
            try:
                handle(db, args.agent, json.load(sys.stdin))
            finally:
                db.close()
        except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error, subprocess.SubprocessError) as err:
            print(f"Vantage hook skipped ({type(err).__name__})", file=sys.stderr)
        return
    db = connect()
    try:
        if args.action == "tag":
            key = (args.agent, args.session, args.turn)
            tags = {}
            for assignment in args.set or [args.clear]:
                name, sep, value = assignment.partition("=")
                if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", name):
                    parser.error("invalid classifier dimension name")
                if args.clear and sep:
                    parser.error("--clear expects a dimension name")
                if args.set and (
                    not sep
                    or not 1 <= len(value) <= 512
                    or any(ord(c) < 32 or ord(c) == 127 for c in value)
                ):
                    parser.error("invalid classifier value")
                tags[name] = value if args.set else None
            with write(db):
                row = db.execute(
                    "SELECT state FROM turns WHERE agent=? AND session=? AND turn=?",
                    key,
                ).fetchone()
                if not row:
                    parser.error("unknown turn")
                if args.set and len(set(json.loads(row[0])["tags"]) | tags.keys()) > 32:
                    parser.error("at most 32 dimensions per turn")
                snapshot(db, key, tags=tags, tag_source=args.source)
            launch(db, False, force=True)
        elif args.action == "status":
            for table in ("sessions", "turns", "outbox"):
                print(table, db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            failures, retry_at, last_ack = db.execute("SELECT failures,retry_at,last_ack FROM sender").fetchone()
            print("consecutive_send_failures", failures)
            print("retry_in_seconds", max(0, round(retry_at-time.time())))
            print("last_ack_unix_seconds", int(last_ack))
            print("pending_recoveries", db.execute("SELECT count(*) FROM recovery_queue").fetchone()[0])
            for name, count in db.execute("SELECT name,count FROM diagnostics"):
                print(name, count)
        else:
            SOURCE = "rollout"
            if args.action == "send":
                with os.fdopen(args.lock_fd, "a") as lock:
                    run_sender(db, lock)
            else:
                launch(db, args.action == "recover", foreground=True, force=True)
    finally:
        db.close()


if __name__ == "__main__":
    main()
