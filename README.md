# Vantage Codex plugin

This repository is the local Codex plugin marketplace for Vantage.

## Test

Run the dependency-free Python test suites:

```sh
./bin/test
```

## Install from this checkout

From the repository root:

```sh
./bin/install-local
```

The installer replaces any existing local `vantage-codex-plugin` marketplace registration,
installs `vantage@vantage-codex-plugin` from this checkout, and prints the active source.
Start a new Codex session after installation so Codex reloads the plugin.

After editing the plugin, run `./bin/install-local` again to refresh the installed copy.

## Install from GitHub

Vantage developers with access to the private repository can install the published
marketplace directly:

```sh
codex plugin marketplace add vantage-sh/vantage-codex-plugin
codex plugin add vantage@vantage-codex-plugin
```

Use the checkout-based installation above while actively developing the plugin.

## Layout

- `.agents/plugins/marketplace.json` declares the local marketplace.
- `plugins/vantage/plugin.json` declares the Vantage plugin.
- `plugins/vantage/skills` contains telemetry setup and session attribution skills.
- `plugins/vantage/server` contains the local MCP tools used to connect or remove telemetry.
- `plugins/vantage/hooks` contains the local Codex hook and launcher.
- `test/plugins/vantage` contains the standalone Python tests.

The Vantage API endpoints used by setup and ingestion remain in Vantage Core. This
repository contains only code that is installed and executed on a Codex user's machine.

Telemetry setup merges Vantage's `SessionStart` and `Stop` handlers into the user's
`~/.codex/hooks.json` and installs a stable launcher in `~/.codex/vantage/hooks`.
On every run, the launcher reads each installed Vantage plugin's declared `version`
from `plugin.json` and executes the newest plugin-owned Python hook in the same isolated
Python process. If no valid hook is installed, it exits successfully without doing
anything. Unrelated user hooks, telemetry configuration, and credentials are preserved.
Setup also stores a read-only classifier credential and the current classifier Markdown
under `~/.codex/vantage` with user-only permissions. At each `SessionStart`, the hook
uses an ETag conditional request to refresh the Markdown and falls back to the last
valid copy when Vantage is unavailable. The attribution skill reads that file instead
of embedding classifier values. Vantage scopes the Markdown to the customer's account,
so its real-time AI sources share one classifier. Classifiers may define custom tag
dimensions; hooks send them under the `vantage.classification.*` namespace without
interpreting their meaning. Removing Vantage telemetry removes
the managed hooks, launcher, classifier credential, and cached classifier.
