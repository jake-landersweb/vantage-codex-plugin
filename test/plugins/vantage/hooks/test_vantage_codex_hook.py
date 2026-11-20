import concurrent.futures
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "plugins/vantage/hooks/vantage_codex_hook.py"
SPEC = importlib.util.spec_from_file_location("vantage_codex_hook", SCRIPT)
assert SPEC and SPEC.loader
HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOOK)


class VantageCodexHookTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="vantage-codex-plugin-hook"
        )
        self.directory = Path(self.temporary_directory.name)
        self.state_directory = self.directory / "state"

    def tearDown(self):
        self.temporary_directory.cleanup()

    def install_cached_classifier(self, codex_home, token=False):
        vantage = Path(codex_home) / "vantage"
        vantage.mkdir(parents=True, exist_ok=True)
        (vantage / "classifier.md").write_text(
            "# Test classifier\n", encoding="utf-8"
        )
        if token:
            (vantage / "classifier_read_token").write_text(
                "classifier-token", encoding="utf-8"
            )

    def write_transcript(self, path, records, mode="w"):
        with path.open(mode, encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")

    def event_attributes(self, body):
        record = body["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
        return {
            attribute["key"]: next(iter(attribute["value"].values()))
            for attribute in record["attributes"]
        }

    def test_prefers_plugin_managed_credential_over_environment_variable(self):
        codex_home = self.directory / "codex"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            """
[otel]
# BEGIN Vantage telemetry (managed by the Vantage plugin; rerun setup to change it)
exporter = { otlp-http = { headers = { authorization = "Bearer vntg_ti_TEST_CREDENTIAL" } } }
# END Vantage telemetry
""".lstrip(),
            encoding="utf-8",
        )
        self.install_cached_classifier(codex_home)
        output = io.StringIO()
        env = {
            "CODEX_HOME": str(codex_home),
            "VANTAGE_CODEX_HOOK_STATE_DIR": str(self.state_directory),
            "VANTAGE_CODEX_INGEST_ENDPOINT": "http://127.0.0.1:4318",
            "VANTAGE_CODEX_INGEST_TOKEN": "vntg_ti_IGNORED_ENVIRONMENT_CREDENTIAL",
        }

        with mock.patch.object(HOOK, "deliver") as deliver:
            HOOK.run(
                stdin=io.StringIO(
                    json.dumps(
                        {"hook_event_name": "SessionStart", "session_id": "session-1"}
                    )
                ),
                env=env,
                stdout=output,
                stderr=io.StringIO(),
            )
            HOOK.run(
                stdin=io.StringIO(
                    json.dumps(
                        {
                            "hook_event_name": "Stop",
                            "session_id": "session-1",
                            "turn_id": "turn-1",
                        }
                    )
                ),
                env=env,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

        self.assertIn("tag --session session-1", output.getvalue())
        self.assertNotIn("vntg_ti_TEST_CREDENTIAL", output.getvalue())
        deliver.assert_called_once()
        self.assertEqual(
            "vntg_ti_TEST_CREDENTIAL", deliver.call_args.kwargs["token"]
        )

    def test_reuses_credential_from_existing_vantage_exporter(self):
        codex_home = self.directory / "codex"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            f"""
[otel]
exporter = {{ otlp-http = {{ endpoint = "{HOOK.INGEST_ENDPOINT}", headers = {{ authorization = "Bearer vntg_ti_EXISTING_CREDENTIAL" }} }} }}
""".lstrip(),
            encoding="utf-8",
        )
        env = {
            "CODEX_HOME": str(codex_home),
            "VANTAGE_CODEX_HOOK_STATE_DIR": str(self.state_directory),
            "VANTAGE_CODEX_INGEST_ENDPOINT": "http://127.0.0.1:4318",
        }

        with mock.patch.object(HOOK, "deliver") as deliver:
            HOOK.run(
                stdin=io.StringIO(
                    json.dumps(
                        {
                            "hook_event_name": "Stop",
                            "session_id": "session-1",
                            "turn_id": "turn-1",
                        }
                    )
                ),
                env=env,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

        deliver.assert_called_once()
        self.assertEqual(
            "vntg_ti_EXISTING_CREDENTIAL", deliver.call_args.kwargs["token"]
        )

    def test_delivers_attribution_with_the_plugin_user_agent(self):
        with mock.patch.object(
            HOOK.urllib.request,
            "urlopen",
            return_value=mock.Mock(status=200),
        ) as urlopen:
            HOOK.deliver(
                {"resourceLogs": []},
                token="vntg_ti_TEST_CREDENTIAL",
                endpoint="https://ingest.vantage.sh",
                stderr=io.StringIO(),
            )

        request = urlopen.call_args.args[0]
        self.assertEqual(HOOK.USER_AGENT, request.get_header("User-agent"))

    def test_session_start_does_not_read_the_telemetry_config(self):
        output = io.StringIO()
        codex_home = self.directory / "codex"
        self.install_cached_classifier(codex_home)

        with mock.patch.object(HOOK, "ingest_token") as ingest_token:
            HOOK.run(
                stdin=io.StringIO(
                    json.dumps(
                        {"hook_event_name": "SessionStart", "session_id": "session-1"}
                    )
                ),
                env={"CODEX_HOME": str(codex_home)},
                stdout=output,
                stderr=io.StringIO(),
            )

        ingest_token.assert_not_called()
        self.assertIn("tag --session session-1", output.getvalue())
        self.assertIn("classifier.md", output.getvalue())

    def test_refreshes_classifier_with_etag_and_private_permissions(self):
        codex_home = self.directory / "codex"
        self.install_cached_classifier(codex_home, token=True)
        (codex_home / "vantage" / "classifier.etag").write_text(
            '"old"', encoding="utf-8"
        )
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            {"classifier_markdown": "# Updated classifier\n"}
        ).encode("utf-8")
        response.headers = {"ETag": '"new"'}
        opener = mock.Mock()
        opener.open.return_value = response
        env = {
            "CODEX_HOME": str(codex_home),
            "VANTAGE_CLASSIFIER_ENDPOINT": "https://api.vantage.sh/plugins/realtime_ai_classifier",
        }

        with mock.patch.object(HOOK.urllib.request, "build_opener", return_value=opener):
            self.assertTrue(HOOK.refresh_classifier(env, stderr=io.StringIO()))

        request = opener.open.call_args.args[0]
        self.assertEqual(
            HOOK.CLASSIFIER_TIMEOUT,
            opener.open.call_args.kwargs["timeout"],
        )
        self.assertEqual("Bearer classifier-token", request.get_header("Authorization"))
        self.assertEqual('"old"', request.get_header("If-none-match"))
        classifier = codex_home / "vantage" / "classifier.md"
        self.assertEqual("# Updated classifier\n", classifier.read_text(encoding="utf-8"))
        self.assertEqual(0o600, classifier.stat().st_mode & 0o777)
        self.assertEqual(
            '"new"',
            (codex_home / "vantage" / "classifier.etag").read_text(encoding="utf-8"),
        )

    def test_classifier_values_are_not_hardcoded_in_the_hook(self):
        status = HOOK.tag(
            [
                "--session",
                "session-1",
                "--tag",
                "work_type=bug_fix",
                "--tag",
                "department=engineering",
            ],
            env={"VANTAGE_CODEX_HOOK_STATE_DIR": str(self.state_directory)},
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )

        self.assertEqual(0, status)

    def test_reclassification_replaces_omitted_custom_tags(self):
        env = {"VANTAGE_CODEX_HOOK_STATE_DIR": str(self.state_directory)}
        self.assertEqual(
            0,
            HOOK.tag(
                ["--session", "session-1", "--tag", "department=engineering"],
                env=env,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            ),
        )
        self.assertEqual(
            0,
            HOOK.tag(
                ["--session", "session-1", "--tag", "work_type=research"],
                env=env,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            ),
        )

        self.assertEqual({"work_type": "research"}, HOOK.skill_tags(env, "session-1"))

    def test_rejects_invalid_custom_tags(self):
        env = {"VANTAGE_CODEX_HOOK_STATE_DIR": str(self.state_directory)}
        for value in ("missing-equals", "Department=engineering", "department=line\nbreak"):
            status = HOOK.tag(
                ["--session", "session-1", "--tag", value],
                env=env,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

            self.assertEqual(1, status)

    def test_does_not_reuse_credential_from_another_exporter(self):
        codex_home = self.directory / "codex"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            """
[otel]
exporter = { otlp-http = { endpoint = "https://example.com/v1/logs", headers = { authorization = "Bearer unrelated-credential" } } }
""".lstrip(),
            encoding="utf-8",
        )
        env = {
            "CODEX_HOME": str(codex_home),
            "VANTAGE_CODEX_HOOK_STATE_DIR": str(self.state_directory),
            "VANTAGE_CODEX_INGEST_ENDPOINT": "http://127.0.0.1:4318",
        }

        with mock.patch.object(HOOK, "deliver") as deliver:
            HOOK.run(
                stdin=io.StringIO(
                    json.dumps(
                        {
                            "hook_event_name": "Stop",
                            "session_id": "session-1",
                            "turn_id": "turn-1",
                        }
                    )
                ),
                env=env,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

        deliver.assert_not_called()

    def test_sends_only_session_attribution(self):
        events_path = self.directory / "events.jsonl"
        env = {
            "VANTAGE_CODEX_EVENTS_PATH": str(events_path),
            "VANTAGE_CODEX_HOOK_STATE_DIR": str(self.state_directory),
        }
        status = HOOK.tag(
            [
                "--session",
                "session-1",
                "--tag",
                "purpose=development",
                "--tag",
                "project=Codex",
                "--tag",
                "source=explicit",
                "--tag",
                "department=engineering",
            ],
            env=env,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        self.assertEqual(0, status)

        HOOK.run(
            stdin=io.StringIO(
                json.dumps(
                    {
                        "hook_event_name": "Stop",
                        "session_id": "session-1",
                        "turn_id": "turn-1",
                        "cwd": "",
                        "transcript_path": "/private/secret/transcript.jsonl",
                        "prompt": "private prompt",
                        "response": "private response",
                    }
                )
            ),
            env=env,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            now_ns=1_763_294_400_000_000_000,
        )

        body = json.loads(events_path.read_text(encoding="utf-8"))
        record = body["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
        attributes = {
            attribute["key"]: next(iter(attribute["value"].values()))
            for attribute in record["attributes"]
        }

        self.assertEqual("codex.turn.attribution", record["eventName"])
        self.assertEqual("session-1", attributes["conversation.id"])
        self.assertEqual("turn-1", attributes["codex.turn.id"])
        self.assertEqual("1", attributes["vantage.turn.revision"])
        self.assertEqual("partial", attributes["vantage.turn.state"])
        self.assertEqual("development", attributes["vantage.classification.purpose"])
        self.assertEqual("Codex", attributes["vantage.classification.project"])
        self.assertEqual("engineering", attributes["vantage.classification.department"])
        self.assertNotIn("vantage.github", attributes)
        self.assertNotIn("vantage.ticket", attributes)
        serialized = json.dumps(body)
        self.assertNotIn("private prompt", serialized)
        self.assertNotIn("private response", serialized)
        self.assertNotIn("/private/secret", serialized)

    def test_emits_incremental_completed_turn_summary_without_content(self):
        transcript = self.directory / "rollout.jsonl"
        self.write_transcript(
            transcript,
            [
                {
                    "timestamp": "2026-11-20T15:00:00.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_started",
                        "turn_id": "turn-1",
                        "root_turn_id": "turn-1",
                        "private": "prompt content",
                    },
                },
                {
                    "timestamp": "2026-11-20T15:00:00.100Z",
                    "type": "turn_context",
                    "payload": {
                        "turn_id": "turn-1",
                        "root_turn_id": "turn-1",
                        "model": "gpt-6.2-sol",
                    },
                },
                {
                    "timestamp": "2026-11-20T15:00:01.000Z",
                    "type": "token_usage_record",
                    "payload": {
                        "turn_id": "turn-1",
                        "root_turn_id": "turn-1",
                        "response_id": "response-1",
                        "turn_token_usage": {
                            "input_tokens": 10,
                            "cached_input_tokens": 4,
                            "cache_write_input_tokens": 1,
                            "output_tokens": 2,
                            "reasoning_output_tokens": 1,
                            "total_tokens": 12,
                        },
                    },
                },
                {
                    "timestamp": "2026-11-20T15:00:02.000Z",
                    "type": "token_usage_record",
                    "payload": {
                        "turn_id": "turn-1",
                        "root_turn_id": "turn-1",
                        "response_id": "response-2",
                        "turn_token_usage": {
                            "input_tokens": 15,
                            "cached_input_tokens": 6,
                            "cache_write_input_tokens": 1,
                            "output_tokens": 5,
                            "reasoning_output_tokens": 2,
                            "total_tokens": 20,
                        },
                    },
                },
                {
                    "timestamp": "2026-11-20T15:00:02.500Z",
                    "type": "turn_context",
                    "payload": {
                        "turn_id": "subturn-1",
                        "root_turn_id": "turn-1",
                        "model": "codex-auto-review",
                    },
                },
                {
                    "timestamp": "2026-11-20T15:00:03.000Z",
                    "type": "token_usage_record",
                    "payload": {
                        "turn_id": "subturn-1",
                        "root_turn_id": "turn-1",
                        "response_id": "response-3",
                        "turn_token_usage": {
                            "input_tokens": 7,
                            "cached_input_tokens": 2,
                            "cache_write_input_tokens": 0,
                            "output_tokens": 3,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 10,
                        },
                    },
                },
                {
                    "timestamp": "2026-11-20T15:00:04.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn-1",
                        "root_turn_id": "turn-1",
                    },
                },
            ],
        )
        events = self.directory / "events.jsonl"
        env = {
            "VANTAGE_CODEX_EVENTS_PATH": str(events),
            "VANTAGE_CODEX_HOOK_STATE_DIR": str(self.state_directory),
        }
        payload = {
            "hook_event_name": "Stop",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "transcript_path": str(transcript),
        }

        HOOK.run(
            stdin=io.StringIO(json.dumps(payload)),
            env=env,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            now_ns=1_795_184_405_000_000_000,
        )
        HOOK.run(
            stdin=io.StringIO(json.dumps(payload)),
            env=env,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            now_ns=1_795_184_406_000_000_000,
        )

        bodies = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(1, len(bodies))
        attributes = self.event_attributes(bodies[0])
        self.assertEqual("completed", attributes["vantage.turn.state"])
        self.assertEqual("1", attributes["vantage.turn.revision"])
        self.assertEqual("3", attributes["codex.turn.request_count"])
        self.assertEqual("22", attributes["codex.turn.input_token_count"])
        self.assertEqual("8", attributes["codex.turn.cached_input_token_count"])
        self.assertEqual("8", attributes["codex.turn.output_token_count"])
        self.assertEqual("30", attributes["codex.turn.total_token_count"])
        self.assertEqual("gpt-6.2-sol", attributes["model"])
        self.assertEqual(
            HOOK.turn_logical_id("session-1", "turn-1"),
            attributes["vantage.turn.logical_id"],
        )
        serialized = json.dumps(bodies[0])
        self.assertNotIn("prompt content", serialized)
        self.assertNotIn(str(transcript), serialized)

    def test_later_turn_recovers_an_interrupted_turn_as_a_new_revision(self):
        first = self.directory / "first.jsonl"
        second = self.directory / "second.jsonl"
        self.write_transcript(
            first,
            [
                {
                    "timestamp": "2026-11-20T15:00:00.000Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-1", "root_turn_id": "turn-1"},
                },
                {
                    "timestamp": "2026-11-20T15:00:01.000Z",
                    "type": "token_usage_record",
                    "payload": {
                        "turn_id": "turn-1",
                        "root_turn_id": "turn-1",
                        "response_id": "response-1",
                        "turn_token_usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
                    },
                },
            ],
        )
        self.write_transcript(
            second,
            [
                {
                    "timestamp": "2026-11-20T16:00:00.000Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-2", "root_turn_id": "turn-2"},
                },
                {
                    "timestamp": "2026-11-20T16:00:01.000Z",
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "turn-2", "root_turn_id": "turn-2"},
                },
            ],
        )
        events = self.directory / "events.jsonl"
        env = {
            "VANTAGE_CODEX_EVENTS_PATH": str(events),
            "VANTAGE_CODEX_HOOK_STATE_DIR": str(self.state_directory),
        }
        for turn_id, transcript in (("turn-1", first), ("turn-2", second)):
            HOOK.run(
                stdin=io.StringIO(
                    json.dumps(
                        {
                            "hook_event_name": "Stop",
                            "session_id": "session-1",
                            "turn_id": turn_id,
                            "transcript_path": str(transcript),
                        }
                    )
                ),
                env=env,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

        attributes = [
            self.event_attributes(json.loads(line))
            for line in events.read_text(encoding="utf-8").splitlines()
        ]
        turn_one = [item for item in attributes if item["codex.turn.id"] == "turn-1"]
        self.assertEqual(["1", "2"], [item["vantage.turn.revision"] for item in turn_one])
        self.assertEqual("interrupted", turn_one[-1]["vantage.turn.state"])
        self.assertTrue(turn_one[-1]["vantage.turn.recovered"])

    def test_subturn_completion_does_not_complete_the_root_turn(self):
        state = {"version": HOOK.TURN_STATE_VERSION, "transcripts": {}, "turns": {}}
        HOOK.apply_transcript_record(
            state,
            "session-1",
            {
                "timestamp": "2026-11-20T15:00:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_started",
                    "turn_id": "turn-1",
                    "root_turn_id": "turn-1",
                },
            },
        )
        HOOK.apply_transcript_record(
            state,
            "session-1",
            {
                "timestamp": "2026-11-20T15:00:01.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "subturn-1",
                    "root_turn_id": "turn-1",
                },
            },
        )

        self.assertEqual("partial", state["turns"]["session-1:turn-1"]["state"])

    def test_a_turn_in_another_session_is_not_marked_interrupted(self):
        events = self.directory / "events.jsonl"
        env = {
            "VANTAGE_CODEX_EVENTS_PATH": str(events),
            "VANTAGE_CODEX_HOOK_STATE_DIR": str(self.state_directory),
        }
        for session_id, turn_id, now_ns in (
            ("session-1", "turn-1", 1_795_184_400_000_000_000),
            ("session-2", "turn-2", 1_795_188_000_000_000_000),
        ):
            HOOK.run(
                stdin=io.StringIO(
                    json.dumps(
                        {
                            "hook_event_name": "Stop",
                            "session_id": session_id,
                            "turn_id": turn_id,
                        }
                    )
                ),
                env=env,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                now_ns=now_ns,
            )

        attributes = [
            self.event_attributes(json.loads(line))
            for line in events.read_text(encoding="utf-8").splitlines()
        ]
        session_one = [
            item for item in attributes if item["codex.turn.id"] == "turn-1"
        ]
        self.assertEqual(1, len(session_one))
        self.assertEqual("partial", session_one[0]["vantage.turn.state"])

    def test_failed_delivery_is_replayed_on_the_next_stop(self):
        codex_home = self.directory / "codex"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            "[otel]\nexporter = { otlp-http = { endpoint = \"https://ingest.vantage.sh/v1/logs\", headers = { authorization = \"Bearer vntg_ti_REPLAY_CREDENTIAL\" } } }\n",
            encoding="utf-8",
        )
        env = {
            "CODEX_HOME": str(codex_home),
            "VANTAGE_CODEX_HOOK_STATE_DIR": str(self.state_directory),
        }
        payload = {"hook_event_name": "Stop", "session_id": "session-1", "turn_id": "turn-1"}
        with mock.patch.object(HOOK, "deliver", side_effect=OSError("offline")):
            HOOK.run(
                stdin=io.StringIO(json.dumps(payload)),
                env=env,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertTrue(HOOK.outbox_path(env).is_file())

        with mock.patch.object(HOOK, "deliver", return_value=mock.Mock(status=200)) as deliver:
            HOOK.run(
                stdin=io.StringIO(json.dumps(payload)),
                env=env,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        deliver.assert_called_once()
        self.assertFalse(HOOK.outbox_path(env).exists())

    def test_session_start_retries_the_outbox_once_with_a_short_timeout(self):
        codex_home = self.directory / "codex"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            "[otel]\nexporter = { otlp-http = { endpoint = \"https://ingest.vantage.sh/v1/logs\", headers = { authorization = \"Bearer vntg_ti_REPLAY_CREDENTIAL\" } } }\n",
            encoding="utf-8",
        )
        env = {
            "CODEX_HOME": str(codex_home),
            "VANTAGE_CODEX_HOOK_STATE_DIR": str(self.state_directory),
        }
        first = HOOK.envelope(
            HOOK.EVENT_NAME, "session-1", None, [], 1, "codex_hook"
        )
        second = HOOK.envelope(
            HOOK.EVENT_NAME, "session-2", None, [], 2, "codex_hook"
        )
        HOOK.save_outbox(env, [first, second])

        with mock.patch.object(
            HOOK, "deliver", return_value=mock.Mock(status=200)
        ) as deliver:
            HOOK.run(
                stdin=io.StringIO(
                    json.dumps(
                        {"hook_event_name": "SessionStart", "session_id": "session-3"}
                    )
                ),
                env=env,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

        deliver.assert_called_once()
        self.assertEqual(
            HOOK.SESSION_RETRY_TIMEOUT, deliver.call_args.kwargs["timeout"]
        )
        self.assertEqual(2, len(deliver.call_args.args[0]["resourceLogs"]))
        self.assertFalse(HOOK.outbox_path(env).exists())

    def test_parallel_stops_preserve_every_turn_and_local_event(self):
        env = {
            "VANTAGE_CODEX_EVENTS_PATH": str(self.directory / "events.jsonl"),
            "VANTAGE_CODEX_HOOK_STATE_DIR": str(self.state_directory),
        }

        def stop(index):
            HOOK.run(
                stdin=io.StringIO(
                    json.dumps(
                        {
                            "hook_event_name": "Stop",
                            "session_id": f"session-{index}",
                            "turn_id": f"turn-{index}",
                        }
                    )
                ),
                env=env,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                now_ns=index + 1,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(stop, range(20)))

        state = HOOK.load_turn_state(env)
        events = (self.directory / "events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(20, len(state["turns"]))
        self.assertEqual(20, len(events))
        self.assertEqual(
            {"1"},
            {
                self.event_attributes(json.loads(event))["vantage.turn.revision"]
                for event in events
            },
        )

    def test_rejects_removed_work_item_tags(self):
        removed_tags = (
            ("--issue", "VAN-3928"),
            ("--pr", "https://github.com/vantage-sh/core/pull/1"),
        )
        for option, value in removed_tags:
            status = HOOK.tag(
                ["--session", "session-1", option, value],
                env={"VANTAGE_CODEX_HOOK_STATE_DIR": str(self.state_directory)},
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

            self.assertEqual(1, status)

    def test_launcher_skips_the_hook_when_python_is_unavailable(self):
        launcher = SCRIPT.parent / "run_vantage_codex_hook.sh"
        environment = os.environ.copy()
        environment.update(
            {
                "PLUGIN_ROOT": str(ROOT / "plugins/vantage"),
                "VANTAGE_CODEX_PYTHON": str(self.directory / "missing-python"),
            }
        )

        result = subprocess.run(
            ["sh", str(launcher)],
            input=json.dumps({"hook_event_name": "Stop", "session_id": "session-1"}),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertEqual("", result.stderr)

    def test_launcher_resolves_an_explicit_plugin_root(self):
        launcher = SCRIPT.parent / "run_vantage_codex_hook.sh"
        environment = os.environ.copy()
        environment.pop("PLUGIN_ROOT", None)
        codex_home = self.directory / "codex"
        self.install_cached_classifier(codex_home)
        environment.update(
            {
                "CODEX_HOME": str(codex_home),
                "VANTAGE_CODEX_PLUGIN_ROOT": str(ROOT / "plugins/vantage"),
                "VANTAGE_CODEX_PYTHON": sys.executable,
            }
        )

        result = subprocess.run(
            ["sh", str(launcher)],
            input=json.dumps({"hook_event_name": "SessionStart", "session_id": "session-1"}),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, result.returncode)
        self.assertIn("tag --session session-1", result.stdout)
        self.assertEqual("", result.stderr)

    def test_launcher_uses_the_newest_declared_plugin_version(self):
        codex_home = self.directory / "codex"
        cache_root = codex_home / "plugins" / "cache" / "workspace-directory" / "vantage"
        old_plugin = cache_root / "999-not-a-version"
        new_plugin = cache_root / "carton-at-the-back"
        old_hooks = old_plugin / "hooks"
        new_hooks = new_plugin / "hooks"
        old_hooks.mkdir(parents=True)
        new_hooks.mkdir(parents=True)
        (old_plugin / "plugin.json").write_text(
            json.dumps({"name": "vantage", "version": "0.3.1"}), encoding="utf-8"
        )
        (new_plugin / "plugin.json").write_text(
            json.dumps({"name": "vantage", "version": "0.3.2"}), encoding="utf-8"
        )
        (old_hooks / "vantage_codex_hook.py").write_text(
            "print('old hook')\n", encoding="utf-8"
        )
        (new_hooks / "vantage_codex_hook.py").write_text(
            "print('new hook')\n", encoding="utf-8"
        )

        environment = os.environ.copy()
        self.install_cached_classifier(codex_home)
        environment.update(
            {
                "CODEX_HOME": str(codex_home),
                "VANTAGE_CODEX_PYTHON": sys.executable,
            }
        )
        result = subprocess.run(
            ["sh", str(SCRIPT.parent / "run_vantage_codex_hook.sh")],
            input=json.dumps({"hook_event_name": "SessionStart", "session_id": "session-1"}),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual("new hook\n", result.stdout)
        self.assertEqual("", result.stderr)

    def test_launcher_does_nothing_when_no_valid_hook_is_installed(self):
        codex_home = self.directory / "codex"
        invalid_plugin = (
            codex_home
            / "plugins"
            / "cache"
            / "workspace-directory"
            / "vantage"
            / "newest-looking-folder"
        )
        (invalid_plugin / "hooks").mkdir(parents=True)
        (invalid_plugin / "plugin.json").write_text(
            json.dumps({"name": "vantage", "version": "not-a-version"}),
            encoding="utf-8",
        )
        (invalid_plugin / "hooks" / "vantage_codex_hook.py").write_text(
            "print('should not run')\n", encoding="utf-8"
        )

        environment = os.environ.copy()
        environment.update(
            {
                "CODEX_HOME": str(codex_home),
                "VANTAGE_CODEX_PYTHON": sys.executable,
            }
        )
        result = subprocess.run(
            ["sh", str(SCRIPT.parent / "run_vantage_codex_hook.sh")],
            input=json.dumps({"hook_event_name": "SessionStart", "session_id": "session-1"}),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertEqual("", result.stderr)

    def test_launcher_ignores_pythonpath_modules(self):
        injected_modules = self.directory / "injected-modules"
        injected_modules.mkdir()
        (injected_modules / "re.py").write_text(
            "raise RuntimeError('loaded injected re.py')\n", encoding="utf-8"
        )

        environment = os.environ.copy()
        codex_home = self.directory / "codex"
        self.install_cached_classifier(codex_home)
        environment.update(
            {
                "CODEX_HOME": str(codex_home),
                "PYTHONPATH": str(injected_modules),
                "VANTAGE_CODEX_PLUGIN_ROOT": str(ROOT / "plugins/vantage"),
                "VANTAGE_CODEX_PYTHON": sys.executable,
            }
        )
        result = subprocess.run(
            ["sh", str(SCRIPT.parent / "run_vantage_codex_hook.sh")],
            input=json.dumps({"hook_event_name": "SessionStart", "session_id": "session-1"}),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, result.returncode)
        self.assertIn("tag --session session-1", result.stdout)
        self.assertEqual("", result.stderr)


if __name__ == "__main__":
    unittest.main()
