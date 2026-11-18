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

The installer replaces any existing local `vantage-plugins` marketplace registration,
installs `vantage@vantage-plugins` from this checkout, and prints the active source.
Start a new Codex session after installation so Codex reloads the plugin and its hooks.

After editing the plugin, run `./bin/install-local` again to refresh the installed copy.

## Install from GitHub

Vantage developers with access to the private repository can install the published
marketplace directly:

```sh
codex plugin marketplace add vantage-sh/vantage-plugins
codex plugin add vantage@vantage-plugins
```

Use the checkout-based installation above while actively developing the plugin.

## Layout

- `.agents/plugins/marketplace.json` declares the local marketplace.
- `plugins/vantage/plugin.json` declares the Vantage plugin.
- `plugins/vantage/skills` contains telemetry setup and session attribution skills.
- `plugins/vantage/hooks` contains the local Codex hook and launcher.
- `test/plugins/vantage` contains the standalone Python tests.

The Vantage API endpoints used by setup and ingestion remain in Vantage Core. This
repository contains only code that is installed and executed on a Codex user's machine.
