import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import urllib.parse
import unittest
from unittest import mock


SCRIPT = (
    pathlib.Path(__file__).parents[4]
    / "plugins/vantage/skills/setup-vantage-telemetry/scripts/configure_vantage_telemetry.py"
)
SPEC = importlib.util.spec_from_file_location("configure_vantage_telemetry", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Response:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class ConfigureVantageTelemetryTest(unittest.TestCase):
    def authorization_expires_at(self):
        return int(MODULE.time.time()) + MODULE.AUTHORIZATION_TIMEOUT

    def run_main(self, config=""):
        with tempfile.TemporaryDirectory() as codex_home:
            path = pathlib.Path(codex_home) / "config.toml"
            if config:
                path.write_text(config, encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, {"CODEX_HOME": codex_home}):
                with mock.patch.object(sys, "argv", [str(SCRIPT)]):
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        status = MODULE.main()

            result = path.read_text(encoding="utf-8") if path.exists() else ""
            return status, stdout.getvalue(), stderr.getvalue(), result

    def test_browser_sign_in_uses_no_localhost_callback(self):
        with mock.patch.object(MODULE.time, "time", return_value=1_000):
            with mock.patch.object(MODULE.sys, "platform", "linux"):
                with mock.patch.object(MODULE.webbrowser, "open", return_value=True) as browser_open:
                    verifier, authorization_expires_at = MODULE.browser_sign_in()

        authorization_url = browser_open.call_args.args[0]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(authorization_url).query)
        expected_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).decode("ascii").rstrip("=")

        self.assertEqual([expected_challenge], query["code_challenge"])
        self.assertEqual(["S256"], query["code_challenge_method"])
        self.assertEqual([str(authorization_expires_at)], query["authorization_expires_at"])
        self.assertEqual(1_000 + MODULE.AUTHORIZATION_TIMEOUT, authorization_expires_at)
        self.assertNotIn("redirect_uri", query)
        self.assertNotIn("state", query)

    def test_browser_sign_in_uses_native_open_on_macos(self):
        completed = mock.Mock(returncode=0)

        with mock.patch.object(MODULE.sys, "platform", "darwin"):
            with mock.patch.object(MODULE.subprocess, "run", return_value=completed) as run:
                with mock.patch.object(MODULE.webbrowser, "open") as browser_open:
                    MODULE.browser_sign_in()

        command = run.call_args.args[0]
        self.assertEqual("open", command[0])
        self.assertTrue(command[1].startswith(MODULE.AUTHORIZATION_ENDPOINT + "?"))
        self.assertEqual(MODULE.subprocess.DEVNULL, run.call_args.kwargs["stdout"])
        self.assertEqual(MODULE.subprocess.DEVNULL, run.call_args.kwargs["stderr"])
        browser_open.assert_not_called()

    def test_browser_sign_in_does_not_print_the_authorization_url(self):
        stdout = io.StringIO()

        with mock.patch.object(MODULE, "open_authorization_url", return_value=False):
            with contextlib.redirect_stdout(stdout):
                with self.assertRaisesRegex(ValueError, "could not be opened"):
                    MODULE.browser_sign_in()

        self.assertEqual("", stdout.getvalue())

    def test_waits_until_vantage_returns_the_ingest_token(self):
        responses = [
            Response(202, {"status": "pending"}),
            Response(201, {"plaintext_token": "secret-token"}),
        ]

        with mock.patch.object(MODULE.urllib.request, "urlopen", side_effect=responses) as urlopen:
            with mock.patch.object(MODULE.time, "sleep") as sleep:
                authorization_expires_at = self.authorization_expires_at()
                token = MODULE.wait_for_ingest_token("v" * 64, authorization_expires_at)

        self.assertEqual("secret-token", token)
        sleep.assert_called_once_with(MODULE.POLL_INTERVAL)
        request = urlopen.call_args_list[0].args[0]
        request_payload = json.loads(request.data)
        self.assertEqual(authorization_expires_at, request_payload["authorization_expires_at"])
        self.assertEqual(MODULE.USER_AGENT, request.get_header("User-agent"))

    def test_verifies_ingest_token_with_the_plugin_user_agent(self):
        with mock.patch.object(
            MODULE.urllib.request,
            "urlopen",
            return_value=Response(200, {}),
        ) as urlopen:
            MODULE.verify_ingest_token("secret-token")

        request = urlopen.call_args.args[0]
        self.assertEqual(MODULE.USER_AGENT, request.get_header("User-agent"))

    def test_backs_off_while_authorization_is_pending(self):
        responses = [
            Response(202, {"status": "pending"}),
            Response(202, {"status": "pending"}),
            Response(202, {"status": "pending"}),
            Response(201, {"plaintext_token": "secret-token"}),
        ]

        with mock.patch.object(MODULE.urllib.request, "urlopen", side_effect=responses):
            with mock.patch.object(MODULE.time, "sleep") as sleep:
                token = MODULE.wait_for_ingest_token("v" * 64, self.authorization_expires_at())

        self.assertEqual("secret-token", token)
        self.assertEqual(
            [mock.call(2), mock.call(4), mock.call(8)],
            sleep.call_args_list,
        )

    def test_retries_rate_limited_polling_with_backoff(self):
        rate_limited = MODULE.urllib.error.HTTPError(
            MODULE.CLAIM_ENDPOINT,
            429,
            "Too Many Requests",
            {},
            None,
        )
        responses = [rate_limited, Response(201, {"plaintext_token": "secret-token"})]

        with mock.patch.object(MODULE.urllib.request, "urlopen", side_effect=responses):
            with mock.patch.object(MODULE.time, "sleep") as sleep:
                token = MODULE.wait_for_ingest_token("v" * 64, self.authorization_expires_at())

        self.assertEqual("secret-token", token)
        sleep.assert_called_once_with(MODULE.POLL_INTERVAL)

    def test_default_command_stops_when_plugin_configuration_exists(self):
        config = "[otel]\n%s\n" % "\n".join(MODULE.managed_block("existing-token", True))

        with mock.patch.object(MODULE, "browser_sign_in") as browser_sign_in:
            status, stdout, stderr, result = self.run_main(config)

        self.assertEqual(0, status)
        self.assertIn("already configured", stdout)
        self.assertIn("Next steps:", stdout)
        self.assertEqual("", stderr)
        self.assertEqual(config, result)
        browser_sign_in.assert_not_called()

    def test_default_command_refreshes_an_existing_vantage_exporter(self):
        config = """[otel]
log_user_prompt = true
log_agent_responses = true
log_guardian_assessments = true
tool_result = { max_bytes = 1048576 }
exporter = { otlp-http = { endpoint = "https://ingest.vantage.sh/v1/logs", headers = { authorization = "Bearer existing-token" } } }
trace_exporter = "none"
"""

        with mock.patch.object(
            MODULE,
            "browser_sign_in",
            return_value=("verifier", self.authorization_expires_at()),
        ):
            with mock.patch.object(MODULE, "wait_for_ingest_token", return_value="new-token"):
                with mock.patch.object(MODULE, "verify_ingest_token"):
                    status, stdout, stderr, result = self.run_main(config)

        self.assertEqual(0, status)
        self.assertIn("Refreshed Vantage telemetry", stdout)
        self.assertIn("Next steps:", stdout)
        self.assertEqual("", stderr)
        self.assertIn('authorization = "Bearer new-token"', result)
        self.assertNotIn("existing-token", result)
        self.assertIn("# BEGIN Vantage telemetry", result)
        self.assertIn("log_user_prompt = false", result)
        self.assertIn("log_agent_responses = false", result)
        self.assertIn("log_guardian_assessments = false", result)
        self.assertIn("tool_result = { max_bytes = 0 }", result)
        self.assertNotIn("log_user_prompt = true", result)
        self.assertNotIn("log_agent_responses = true", result)
        self.assertIn('trace_exporter = "none"', result)

    def test_default_command_configures_telemetry(self):
        with mock.patch.object(
            MODULE,
            "browser_sign_in",
            return_value=("verifier", self.authorization_expires_at()),
        ):
            with mock.patch.object(MODULE, "wait_for_ingest_token", return_value="new-token"):
                with mock.patch.object(MODULE, "verify_ingest_token"):
                    status, stdout, stderr, result = self.run_main()

        self.assertEqual(0, status)
        self.assertIn("Configured Vantage telemetry", stdout)
        self.assertIn("Next steps:", stdout)
        self.assertEqual("", stderr)
        self.assertIn("[otel]", result)
        self.assertIn('authorization = "Bearer new-token"', result)

    def test_default_command_reports_a_conflicting_exporter(self):
        config = """[otel]
exporter = { otlp-http = { endpoint = "https://example.com/v1/logs" } }
"""

        with mock.patch.object(MODULE, "browser_sign_in") as browser_sign_in:
            status, stdout, stderr, result = self.run_main(config)

        self.assertEqual(3, status)
        self.assertEqual("", stdout)
        self.assertIn("No changes were made", stderr)
        self.assertIn("run this command again", stderr)
        self.assertEqual(config, result)
        browser_sign_in.assert_not_called()

    def test_installs_user_hooks_without_replacing_existing_hooks(self):
        with tempfile.TemporaryDirectory() as codex_home:
            hooks_path = pathlib.Path(codex_home) / "hooks.json"
            existing = {
                "hooks": {
                    "PostToolUse": [{"hooks": [{"type": "command", "command": "echo existing"}]}],
                    "SessionStart": [{"hooks": [{"type": "command", "command": "echo start"}]}],
                }
            }
            hooks_path.write_text(json.dumps(existing), encoding="utf-8")

            with mock.patch.dict(os.environ, {"CODEX_HOME": codex_home}):
                MODULE.install_user_hooks()
                MODULE.install_user_hooks()
                payload = json.loads(hooks_path.read_text(encoding="utf-8"))
                self.assertEqual(existing["hooks"]["PostToolUse"], payload["hooks"]["PostToolUse"])
                self.assertEqual("echo start", payload["hooks"]["SessionStart"][0]["hooks"][0]["command"])
                for event_name in ("SessionStart", "Stop"):
                    managed = [
                        handler
                        for group in payload["hooks"][event_name]
                        for handler in group.get("hooks", [])
                        if MODULE.is_managed_hook(handler)
                    ]
                    self.assertEqual(1, len(managed))

            installed = pathlib.Path(codex_home) / "vantage" / "hooks"
            for name in MODULE.HOOK_SCRIPT_NAMES:
                destination = installed / name
                self.assertEqual(
                    (MODULE.BUNDLED_HOOKS_DIR / name).read_bytes(),
                    destination.read_bytes(),
                )
                self.assertTrue(destination.stat().st_mode & 0o100)

    def test_remove_user_hooks_preserves_unrelated_hooks(self):
        with tempfile.TemporaryDirectory() as codex_home:
            hooks_path = pathlib.Path(codex_home) / "hooks.json"
            with mock.patch.dict(os.environ, {"CODEX_HOME": codex_home}):
                MODULE.install_user_hooks()
                payload = json.loads(hooks_path.read_text(encoding="utf-8"))
                payload["hooks"]["Stop"].insert(
                    0,
                    {"hooks": [{"type": "command", "command": "echo keep-me"}]},
                )
                hooks_path.write_text(json.dumps(payload), encoding="utf-8")

                self.assertTrue(MODULE.remove_user_hooks())

            payload = json.loads(hooks_path.read_text(encoding="utf-8"))
            self.assertEqual("echo keep-me", payload["hooks"]["Stop"][0]["hooks"][0]["command"])
            self.assertNotIn("SessionStart", payload["hooks"])
            for name in MODULE.HOOK_SCRIPT_NAMES:
                self.assertFalse((pathlib.Path(codex_home) / "vantage" / "hooks" / name).exists())

    def test_invalid_user_hooks_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as codex_home:
            hooks_path = pathlib.Path(codex_home) / "hooks.json"
            hooks_path.write_text("{invalid", encoding="utf-8")

            with mock.patch.dict(os.environ, {"CODEX_HOME": codex_home}):
                with self.assertRaisesRegex(ValueError, "could not be read safely"):
                    MODULE.install_user_hooks()

            self.assertEqual("{invalid", hooks_path.read_text(encoding="utf-8"))
            self.assertFalse((pathlib.Path(codex_home) / "vantage").exists())

if __name__ == "__main__":
    unittest.main()
