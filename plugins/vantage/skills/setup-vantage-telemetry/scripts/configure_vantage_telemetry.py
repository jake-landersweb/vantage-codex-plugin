#!/usr/bin/env python3
"""Configure Codex OpenTelemetry log export to Vantage."""

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser


BEGIN = "# BEGIN Vantage telemetry (managed by the Vantage plugin; rerun setup to change it)"
END = "# END Vantage telemetry"
INGEST_ENDPOINT = os.environ.get("VANTAGE_CODEX_INGEST_ENDPOINT", "https://ingest.vantage.sh/v1/logs")
CLAIM_ENDPOINT = os.environ.get(
    "VANTAGE_CODEX_CLAIM_ENDPOINT", "https://api.vantage.sh/plugins/codex_telemetry_setup/claim"
)
AUTHORIZATION_ENDPOINT = os.environ.get(
    "VANTAGE_CODEX_AUTHORIZATION_ENDPOINT", "https://console.vantage.sh/codex/telemetry/authorize"
)
AUTHORIZATION_TIMEOUT = 600
POLL_INTERVAL = 2
MAX_POLL_INTERVAL = 10
REQUEST_TIMEOUT = max(1, min(300, float(os.environ.get("VANTAGE_CODEX_REQUEST_TIMEOUT", "15"))))
MANAGED_KEYS = (
    "exporter",
    "log_user_prompt",
    "log_agent_responses",
    "log_guardian_assessments",
    "tool_result",
)
TABLE = re.compile(r"^\s*\[")
OTEL_TABLE = re.compile(r"^\s*\[\s*otel\s*\]\s*(?:#.*)?$")
OTEL_CONFIG = re.compile(r"^\s*(?:\[+\s*otel(?:\.|\s*\])|otel(?:\.|\s*=))")
TOKEN = re.compile(r'authorization\s*=\s*"Bearer ([A-Za-z0-9._~+/=\-]{8,512})"')


def config_path():
    codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return os.path.join(codex_home, "config.toml")


def remove_managed_block(text):
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line.strip().startswith("# BEGIN Vantage telemetry")]
    ends = [index for index, line in enumerate(lines) if line.strip().startswith(END)]
    if not starts and not ends:
        return lines, False
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        raise ValueError("the Vantage telemetry block markers are invalid")
    del lines[starts[0] : ends[0] + 1]
    return lines, True


def otel_section(lines):
    headers = [index for index, line in enumerate(lines) if OTEL_TABLE.match(line)]
    if len(headers) > 1:
        raise ValueError("config.toml has more than one [otel] section")
    if not headers:
        return None, None
    start = headers[0]
    end = next((index for index in range(start + 1, len(lines)) if TABLE.match(lines[index])), len(lines))
    return start, end


def inspect_otel(lines):
    start, end = otel_section(lines)
    conflicts = set()
    has_environment = False

    for index, line in enumerate(lines):
        if start is not None and start < index < end:
            for key in MANAGED_KEYS:
                if re.match(r"^\s*%s\s*=" % re.escape(key), line):
                    conflicts.add(key)
            has_environment = has_environment or bool(re.match(r"^\s*environment\s*=", line))
            continue

        for key in MANAGED_KEYS:
            pattern = r"^\s*(?:otel\.%s\s*=|\[+\s*otel\.%s(?:\.|\s*\]))" % (key, key)
            if re.match(pattern, line):
                conflicts.add(key)
        has_environment = has_environment or bool(re.match(r"^\s*otel\.environment\s*=", line))

    if start is None and any(OTEL_CONFIG.match(line) for line in lines):
        conflicts.add("existing otel configuration")
    return start, has_environment, sorted(conflicts)


def vantage_exporter_token(lines):
    start, end = otel_section(lines)
    candidates = []

    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#") or INGEST_ENDPOINT not in line:
            continue

        in_otel = start is not None and start < index < end
        assignment = r"^\s*exporter\s*=" if in_otel else r"^\s*otel\.exporter\s*="
        if not re.match(assignment, line):
            continue

        match = TOKEN.search(line)
        if match:
            candidates.append(match.group(1))

    return candidates[0] if len(set(candidates)) == 1 else None


def has_vantage_endpoint(lines):
    return any(INGEST_ENDPOINT in line and not line.lstrip().startswith("#") for line in lines)


def remove_managed_settings(lines):
    start, end = otel_section(lines)
    result = []

    for index, line in enumerate(lines):
        in_otel = start is not None and start < index < end
        if in_otel and any(re.match(r"^\s*%s\s*=" % re.escape(key), line) for key in MANAGED_KEYS):
            continue
        if any(re.match(r"^\s*otel\.%s\s*=" % re.escape(key), line) for key in MANAGED_KEYS):
            continue
        result.append(line)

    return result


def managed_block(token, include_environment):
    lines = [BEGIN]
    if include_environment:
        lines.append('environment = "prod"')
    lines += [
        'exporter = { otlp-http = { endpoint = "%s", protocol = "binary", headers = { authorization = "Bearer %s" } } }'
        % (INGEST_ENDPOINT, token),
        "log_user_prompt = false",
        "log_agent_responses = false",
        "log_guardian_assessments = false",
        "tool_result = { max_bytes = 0 }",
        END,
    ]
    return lines


def browser_sign_in():
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    query = urllib.parse.urlencode(
        {
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    authorization_url = "%s?%s" % (AUTHORIZATION_ENDPOINT, query)

    print("Opening Vantage to sign in and connect Codex telemetry...")
    if not webbrowser.open(authorization_url, new=1):
        print("Open this URL to continue:\n%s" % authorization_url)
    return code_verifier


def wait_for_ingest_token(code_verifier):
    deadline = time.monotonic() + AUTHORIZATION_TIMEOUT
    poll_interval = POLL_INTERVAL
    while True:
        request = urllib.request.Request(
            CLAIM_ENDPOINT,
            data=json.dumps({"code_verifier": code_verifier}).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if response.status == 202:
                    if time.monotonic() >= deadline:
                        raise ValueError("Vantage sign-in timed out. Start setup again.")
                    time.sleep(poll_interval)
                    poll_interval = min(MAX_POLL_INTERVAL, poll_interval * 2)
                    continue
        except urllib.error.HTTPError as error:
            if error.code == 429:
                if time.monotonic() >= deadline:
                    raise ValueError("Vantage setup was rate limited. Wait a minute and start setup again.") from error
                time.sleep(poll_interval)
                poll_interval = min(MAX_POLL_INTERVAL, poll_interval * 2)
                continue
            try:
                detail = json.loads(error.read().decode("utf-8")).get(
                    "errors", ["Setup could not be completed."]
                )[0]
            except (json.JSONDecodeError, AttributeError, IndexError, TypeError):
                detail = "Setup could not be completed."
            raise ValueError(detail) from error
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            raise ValueError("Vantage could not be reached to complete setup.") from error

        token = payload.get("plaintext_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise ValueError("Vantage did not return an ingest credential.")
        return token.strip()


def verify_ingest_token(token):
    deadline = time.monotonic() + 5
    while True:
        request = urllib.request.Request(
            INGEST_ENDPOINT,
            data=json.dumps({"resourceLogs": []}).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer %s" % token,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT):
                return
        except urllib.error.HTTPError as error:
            if error.code in (401, 403) and time.monotonic() < deadline:
                time.sleep(0.25)
                continue
            if error.code in (401, 403):
                raise ValueError("Vantage rejected the new ingest credential.") from error
            raise ValueError("Vantage could not verify the new ingest credential.") from error
        except (OSError, urllib.error.URLError) as error:
            raise ValueError("Vantage could not be reached to verify the new ingest credential.") from error


def write_config(path, lines):
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".config.toml.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as output:
            output.write("\n".join(lines).rstrip("\n") + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def print_next_steps():
    print("Next steps:")
    print("1. Review and trust the Vantage hooks in /hooks or Settings > Hooks.")
    print("2. Restart Codex.")
    print("3. Start a new conversation.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remove", action="store_true", help="remove the Vantage-managed block")
    args = parser.parse_args()

    path = config_path()
    original = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            original = handle.read()

    try:
        lines, configured = remove_managed_block(original)
        otel_header, has_environment, conflicts = inspect_otel(lines)
        existing_vantage_token = vantage_exporter_token(lines)
        existing_vantage_endpoint = has_vantage_endpoint(lines)
    except ValueError as error:
        print("Could not read %s: %s" % (path, error), file=sys.stderr)
        return 1

    if args.remove:
        if not configured:
            if existing_vantage_token:
                print("The Vantage configuration in %s is not managed by the plugin. No changes were made." % path)
            else:
                print("Nothing to remove: no Vantage block in %s" % path)
            return 0
        write_config(path, lines)
        print("Removed Vantage telemetry from %s. Restart Codex to apply." % path)
        return 0

    if configured:
        print("Vantage telemetry is already configured in %s." % path)
        print_next_steps()
        return 0

    if existing_vantage_token:
        lines = remove_managed_settings(lines)
        try:
            otel_header, has_environment, remaining_conflicts = inspect_otel(lines)
        except ValueError as error:
            print("Could not safely normalize %s: %s" % (path, error), file=sys.stderr)
            return 3
        if remaining_conflicts or has_vantage_endpoint(lines):
            print(
                "Could not safely normalize the existing Vantage exporter in %s. No changes were made." % path,
                file=sys.stderr,
            )
            return 3
        try:
            code_verifier = browser_sign_in()
            token = wait_for_ingest_token(code_verifier)
            verify_ingest_token(token)
        except ValueError as error:
            print("Could not complete Vantage setup: %s" % error, file=sys.stderr)
            return 2
        block = managed_block(token, not has_environment)
        if otel_header is None:
            lines += ([""] if lines else []) + ["[otel]"] + block
        else:
            lines[otel_header + 1 : otel_header + 1] = block
        write_config(path, lines)
        print("Refreshed Vantage telemetry in %s." % path)
        print_next_steps()
        return 0
    if conflicts or existing_vantage_endpoint:
        if existing_vantage_endpoint:
            print(
                "The Vantage exporter in %s is incomplete or uses an unsupported format. No changes were made." % path,
                file=sys.stderr,
            )
        elif conflicts:
            message = "Automatic setup cannot replace existing OpenTelemetry settings in %s: %s. No changes were made."
            print(message % (path, ", ".join(conflicts)), file=sys.stderr)
        print("Next step: remove or change the conflicting OpenTelemetry settings, then run this command again.", file=sys.stderr)
        return 3
    try:
        code_verifier = browser_sign_in()
        token = wait_for_ingest_token(code_verifier)
        verify_ingest_token(token)
    except ValueError as error:
        print("Could not complete Vantage setup: %s" % error, file=sys.stderr)
        return 2

    block = managed_block(token, not has_environment)
    if otel_header is None:
        lines += ([""] if lines else []) + ["[otel]"] + block
    else:
        lines[otel_header + 1 : otel_header + 1] = block
    write_config(path, lines)
    print("Configured Vantage telemetry in %s." % path)
    print_next_steps()
    return 0


if __name__ == "__main__":
    sys.exit(main())
