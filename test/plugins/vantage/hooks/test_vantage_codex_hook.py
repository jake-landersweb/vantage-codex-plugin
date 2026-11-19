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

        with mock.patch.object(HOOK, "ingest_token") as ingest_token:
            HOOK.run(
                stdin=io.StringIO(
                    json.dumps(
                        {"hook_event_name": "SessionStart", "session_id": "session-1"}
                    )
                ),
                env={},
                stdout=output,
                stderr=io.StringIO(),
            )

        ingest_token.assert_not_called()
        self.assertIn("tag --session session-1", output.getvalue())

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
                "--purpose",
                "development",
                "--project",
                "Codex",
                "--source",
                "explicit",
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
            attribute["key"]: attribute["value"]["stringValue"]
            for attribute in record["attributes"]
        }

        self.assertEqual("codex.turn.attribution", record["eventName"])
        self.assertEqual("session-1", attributes["conversation.id"])
        self.assertEqual("turn-1", attributes["codex.turn.id"])
        self.assertEqual("development", attributes["vantage.purpose"])
        self.assertEqual("Codex", attributes["vantage.project"])
        self.assertNotIn("vantage.github", attributes)
        self.assertNotIn("vantage.ticket", attributes)
        serialized = json.dumps(body)
        self.assertNotIn("private prompt", serialized)
        self.assertNotIn("private response", serialized)
        self.assertNotIn("/private/secret", serialized)

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
        environment.update(
            {
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
            json.dumps({"name": "vantage", "version": "0.4.0"}), encoding="utf-8"
        )
        (old_hooks / "vantage_codex_hook.py").write_text(
            "print('old hook')\n", encoding="utf-8"
        )
        (new_hooks / "vantage_codex_hook.py").write_text(
            "print('new hook')\n", encoding="utf-8"
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
        environment.update(
            {
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
