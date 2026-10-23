# Initial public release scope

This repository starts from a reviewed file allowlist and a new root commit.
It contains the verification engine, tests, public documentation, a minimal Compose
example, and eight curated Hermes skills. It excludes prior Git history, local
agent settings, checkpoint transcripts, runtime state, historical incident notes,
and private project documentation. `RELEASE_MANIFEST.txt` records the initial
publication file set.

## Supported evidence

The local release checks cover a fresh virtual-environment installation, the unit
and regression suite, CLI help entry points, the installer against disposable
folders, documentation links, static Compose validation, lint, formatting, and
secret scanning. Secret scanners detect known patterns; they are not proof that
all private information has been identified.

## Early release limitations

Live Compose acceptance and a clean-install Hermes dispatcher workflow remain
unverified. The two live-host tests require explicit opt-in. GitHub Actions checks
Python 3.11, 3.12, and 3.13 on Linux; inspect the actual run before relying on that
matrix. No Windows or hosted deployment support is claimed.

The engine runs project tests on the host. It is not a sandbox for malicious
repositories. Protected project names are examples to configure for your host.
See [architecture](architecture.md) and [the quickstart](quickstart.md) before use.
