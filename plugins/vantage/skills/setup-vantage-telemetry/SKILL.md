---
name: setup-vantage-telemetry
description: Configure, update, or remove Codex OpenTelemetry usage log export to Vantage.
---

# Set up Vantage telemetry

Before running `scripts/configure_vantage_telemetry.sh`, resolved relative to this file, request permission to run it outside the sandbox. The script needs host access to open the user's default browser and network access to complete setup with Vantage. Do not try it inside the sandbox first. Report only whether setup completed and any concise next step; never relay command output, authorization URLs, or credentials.

Only when the user explicitly asks to remove Vantage telemetry, run the script with `--remove` and relay its output verbatim.
