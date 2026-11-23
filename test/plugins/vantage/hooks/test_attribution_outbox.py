from contextlib import redirect_stdout
import io
import os
import datetime
import json
import time
import sys
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[4] / "plugins/vantage/hooks"),
)
import vantage_codex_hook as store

launch = store.launch
handle, recover, drain = store.handle, store.recover, store.drain


class HookTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        state = patch.object(store, "STATE", self.root / "state")
        state.start()
        self.addCleanup(state.stop)
        kick = patch.object(store, "launch")
        kick.start()
        self.addCleanup(kick.stop)
        self.db = store.connect()
        self.addCleanup(self.db.close)

    def payloads(self):
        return [
            json.loads(row[0])
            for row in self.db.execute("SELECT payload FROM outbox ORDER BY rowid")
        ]

    def submit(self, turn, session="s", branch="feature", head="abc", worktree="work", cwd=None):
        payload = {"session_id": session, "turn_id": turn, "hook_event_name": "UserPromptSubmit",
                   "cwd": str(cwd or self.root), "cli_version": "test"}
        classifier = self.root / "classifier.md"
        facts = {"worktree": worktree, "branch": branch, "head": head} if worktree else {}
        with patch.dict(os.environ, VANTAGE_CLASSIFIER_FILE=str(classifier)), patch.object(
            store, "git_facts", return_value=facts
        ), redirect_stdout(io.StringIO()) as output:
            handle(self.db, "codex", payload)
        return json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]

    def tag(self, turn, *args):
        with patch.object(store, "load_connection"), patch.object(
            sys, "argv", ["hook", "tag", "codex", "s", turn, *args]
        ):
            store.main()

    def test_reuse_survives_commit_and_retry_preserves_override(self):
        self.submit("first")
        self.tag("first", "--set", "pr=10", "--source", "explicit")
        self.tag("first", "--set", "purpose=development", "--source", "inferred")
        self.assertIn("Current attribution", self.submit("next", head="new"))
        state = json.loads(self.payloads()[-1]["state"])
        self.assertEqual(state["tags"], {"pr": "10", "purpose": "development"})
        self.assertEqual(state["tag_sources"], {"pr": "explicit", "purpose": "inferred"})
        self.tag("next", "--replace", "purpose=research")
        self.assertIn('"purpose": "research"', self.submit("next"))
        self.assertEqual(json.loads(self.payloads()[-1]["state"])["tags"], {"purpose": "research"})
        self.tag("next", "--replace")
        self.assertIn("Current attribution: {}", self.submit("third"))

    def test_context_changes_and_unclassified_turns_require_classification(self):
        classifier = self.root / "classifier.md"
        classifier.write_text("v1")
        for i, (options, reuse) in enumerate([
            ({}, False), ({}, False),  # first turn wasn't classified
            ({"branch": "other"}, False), ({"worktree": "other"}, False),
            ({"branch": ""}, False), ({"branch": "", "head": "new"}, False),
            ({"worktree": ""}, False), ({"worktree": ""}, True),
            ({"worktree": "", "cwd": self.root / "other"}, False),
            ({}, False), ({}, False), ({"session": "other"}, False),
        ]):
            with self.subTest(i=i, options=options):
                if i == 10:
                    classifier.write_text("v2")
                turn = str(i)
                self.assertEqual("Current attribution" in self.submit(turn, **options), reuse)
                if i and "session" not in options:
                    self.tag(turn, "--replace", "purpose=research")

    def test_grpc_ack_requires_success_trailer_and_full_batch(self):
        headers = (
            b"HTTP/2 200 \r\ncontent-type: application/grpc\r\n\r\ngrpc-status: 0\r\n"
        )
        empty = bytes(5)
        store.require_ack(headers, empty)
        store.require_ack(headers, bytes.fromhex("00000000020a00"))
        for head, frame in [
            (headers.replace(b"200", b"500"), empty),
            (headers.replace(b"grpc-status: 0", b"grpc-status: 14"), empty),
            (b"HTTP/2 200", empty),
            (headers, bytes.fromhex("00000000040a020801")),  # one rejected log
            (headers, b""),
        ]:
            with self.subTest(head=head, frame=frame), self.assertRaises(RuntimeError):
                store.require_ack(head, frame)

    def test_first_model_request_belongs_to_child_and_tag_survives_stop(self):
        p = {
            "session_id": "parent",
            "agent_id": "child",
            "turn_id": "turn",
            "hook_event_name": "PostModelRequest",
            "token_usage": {"input_tokens": 18},
            "status": "completed",
            "prompt": "never forward this",
        }
        handle(self.db, "codex", p)
        events = self.payloads()
        self.assertEqual(
            [row["event.name"] for row in events], [store.TURN, store.REQUEST]
        )
        self.assertEqual(events[1]["session.id"], "child")
        self.assertNotIn("never forward", json.dumps(events))
        key = ("codex", "child", "turn")
        with store.write(self.db):
            store.snapshot(self.db, key, tags={"purpose": "research"})
        handle(self.db, "codex", dict(p, hook_event_name="Stop"))
        self.assertEqual(
            json.loads(self.payloads()[-1]["state"])["tags"], {"purpose": "research"}
        )
        with store.write(self.db):
            store.snapshot(self.db, key, tags={"purpose": None})
        self.assertEqual(json.loads(self.payloads()[-1]["state"])["tags"], {})

    def test_git_identity_is_frozen_at_observation_for_live_late_and_recovered_events(
        self,
    ):
        subprocess.run(["git", "init", "-q", self.root], check=True)

        def configure(email):
            subprocess.run(
                ["git", "-C", self.root, "config", "user.email", email], check=True
            )

        p = {"session_id": "a", "turn_id": "b", "cwd": str(self.root)}
        key = ("codex", "a", "b")
        configure("git-a@example.invalid")
        handle(self.db, "codex", dict(p, hook_event_name="UserPromptSubmit"))
        handle(self.db, "codex", dict(p, hook_event_name="PostModelRequest"))
        earlier = self.db.execute("SELECT max(timestamp) FROM outbox").fetchone()[0]
        configure("git-b@example.invalid")
        handle(self.db, "codex", dict(p, hook_event_name="Stop"))
        with store.write(self.db):
            store.snapshot(self.db, key, tags={"ticket": "A-1"})
            for then in (1, earlier):
                store.correlation(
                    self.db, key, {}, "rollout", status="completed", timestamp=then
                )
        self.assertEqual(
            [e.get("git.user.email") for e in self.payloads()],
            [
                "git-a@example.invalid",
                "git-a@example.invalid",
                "git-b@example.invalid",
                "git-b@example.invalid",
                None,
                "git-a@example.invalid",
            ],
        )
        self.assertEqual(self.payloads()[-2]["git.identity.source"], "unknown")
        configure("invalid input")
        handle(self.db, "codex", dict(p, hook_event_name="PostModelRequest"))
        self.assertNotIn("git.user.email", self.payloads()[-1])

    def test_request_model_switch_survives_retry(self):
        p = {"session_id": "s", "turn_id": "t", "hook_event_name": "PostModelRequest",
             "cli_version": "0.171.0", "status": "completed"}
        for model in ("first", "second"):
            handle(self.db, "codex", dict(p, model=model))
        handle(self.db, "codex", dict(p, hook_event_name="Stop", cli_version="0.172.0"))
        events = self.payloads()
        requests = [e for e in events if e["event.name"] == store.REQUEST]
        self.assertEqual([e["model"] for e in requests], ["first", "second"])
        self.assertTrue(all(e["agent.version"] == "0.171.0" for e in requests))
        self.assertEqual(events[-1]["model"], "multiple")
        with patch.object(store, "export", side_effect=RuntimeError), self.assertRaises(RuntimeError):
            drain(self.db)
        self.assertEqual(self.payloads(), events)

    def test_long_git_history_keeps_first_latest_and_tag_revision(self):
        key = ("codex", "s", "t")
        for i in range(160):
            store.snapshot(self.db, key, facts={"head": str(i), "branch": "feature-" + "a"*450})
        store.snapshot(self.db, key, tags={"ticket":"PLA-123"})
        encoded = self.payloads()[-1]["state"]
        self.assertLessEqual(len(encoded.encode()), 65536)
        state = json.loads(encoded)
        self.assertEqual(state["git"][0]["facts"]["head"], "0")
        self.assertEqual(state["git"][-1]["facts"]["head"], "159")
        self.assertGreater(state["omitted_git_observations"], 0)
        self.assertEqual(state["tags"]["ticket"], "PLA-123")

    def test_recover_unregistered_post_install_session(self):
        when = datetime.datetime.now(datetime.timezone.utc)
        ms = int(when.timestamp() * 1000)
        session = str(uuid.UUID(int=(ms << 80) | (7 << 76) | (2 << 62)))
        folder = self.root / "sessions"
        folder.mkdir()
        record = {"timestamp": when.isoformat(), "type": "token_usage_record",
                  "payload": {"thread_id": session, "turn_id": "t", "usage": {"output_tokens": 8}}}
        (folder / f"rollout-{session}.jsonl").write_text(json.dumps(record) + "\n")
        with patch.object(store, "HOME", self.root), patch.object(store, "OPT_IN", (ms-1)*1_000_000):
            recover(self.db)
            recover(self.db)
        self.assertEqual([event["event.name"] for event in self.payloads()], [store.TURN, store.REQUEST])

    def test_recovery_isolates_unreadable_file_and_keeps_cursor(self):
        folder = self.root / "sessions"
        folder.mkdir()
        sessions = [str(uuid.uuid4()) for _ in range(2)]
        paths = []
        for session in sessions:
            self.db.execute("INSERT INTO sessions VALUES ('codex',?,?,0)", (session, str(self.root)))
            path = folder / f"rollout-{session}.jsonl"
            paths.append(path)
            record = {"timestamp": "2026-01-01T00:00:00Z", "type": "token_usage_record",
                      "payload": {"thread_id": session, "turn_id": "t", "usage": {"input_tokens": 8}}}
            path.write_text(json.dumps(record) + "\n")
        original_open = Path.open
        def selective_open(path, *args, **kwargs):
            if path == paths[0]:
                raise PermissionError()
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", selective_open):
            recover(self.db)
        self.assertEqual({e["session.id"] for e in self.payloads()}, {sessions[1]})
        self.assertEqual(self.db.execute("SELECT offset FROM sessions WHERE session=?", (sessions[0],)).fetchone()[0], 0)
        recover(self.db)
        self.assertEqual(len(self.payloads()), 4)

    def test_resume_refreshes_version_without_rewriting_historical_requests(self):
        payload = {"session_id": "session", "hook_event_name": "SessionStart"}
        with patch.object(store, "installed_version", side_effect=["1.0", "2.0"]) as lookup:
            handle(self.db, "codex", payload)
            handle(self.db, "codex", dict(payload, hook_event_name="PostModelRequest", turn_id="a"))
            original_time = time.time_ns()
            handle(self.db, "codex", payload)
            handle(self.db, "codex", dict(payload, hook_event_name="PostModelRequest", turn_id="b"))
            store.correlation(self.db, ("codex", "session", "a"), {}, "rollout",
                              status="completed", timestamp=original_time)
        self.assertEqual(lookup.call_count, 2)
        self.assertEqual([e["agent.version"] for e in self.payloads() if e["event.name"] == store.REQUEST],
                         ["1.0", "2.0", "1.0"])

    def test_busy_sender_coalesces_kicks_and_retries_same_id(self):
        key = ("codex", "s", "t")
        store.enqueue(self.db, store.REQUEST, key, {"status":"completed"})
        initial = self.payloads()
        with (store.STATE / "worker.lock").open("a") as lock:
            store.fcntl.flock(lock, store.fcntl.LOCK_EX)
            with patch.object(store.subprocess, "Popen") as spawn:
                for _ in range(20):
                    launch(self.db, True)
                spawn.assert_not_called()
            self.assertEqual(self.db.execute("SELECT count(*) FROM recovery_queue").fetchone()[0], 1)
            with patch.object(store, "recover") as scan, patch.object(store, "export", side_effect=RuntimeError):
                store.run_sender(self.db, lock)
                scan.assert_called_once()
        self.assertEqual(initial, self.payloads())
        with patch.object(store, "export") as send:
            launch(self.db, foreground=True, force=True)
        self.assertEqual(send.call_args.args[1][0][1]["event_id"], initial[0]["event_id"])
        self.assertFalse(self.payloads())

    def test_recovery_handles_partial_lines_parent_history_and_retries(self):
        session = str(uuid.uuid4())
        self.db.execute(
            "INSERT INTO sessions VALUES ('codex',?,?,0)", (session, str(self.root))
        )
        folder = self.root / "sessions"
        folder.mkdir()
        path = folder / f"rollout-{session}.jsonl"
        row = {
            "type": "token_usage_record",
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "payload": {
                "thread_id": session,
                "turn_id": "a",
                "usage": {"input_tokens": 4},
            },
        }
        ancestor = dict(row, payload=dict(row["payload"], thread_id="other"))
        path.write_text(json.dumps(ancestor) + "\n" + json.dumps(row))
        recover(self.db)
        self.assertEqual(self.payloads(), [])
        with path.open("a") as f:
            f.write("\n")
        recover(self.db)
        events = self.payloads()
        self.assertEqual(len(events), 2)
        recover(self.db)
        self.assertEqual(self.payloads(), events)
        with (
            patch.object(store, "export", side_effect=RuntimeError("offline")),
            self.assertRaises(RuntimeError),
        ):
            drain(self.db)
        self.assertEqual(self.payloads(), events)
        with patch.object(store, "export"):
            drain(self.db)
        self.assertEqual(self.payloads(), [])


if __name__ == "__main__":
    unittest.main()
