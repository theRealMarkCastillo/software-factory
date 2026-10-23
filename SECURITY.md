# Security

Do not post credentials, exploit payloads containing private data, or confidential
logs in public issues. Once enabled on GitHub, use private vulnerability reporting
under this repository's Security tab. Until that channel is enabled, arrange a
private reporting channel with the maintainer before sending sensitive details.

Report the affected revision, entry point, prerequisites, expected boundary, and a
minimal reproduction using disposable resources. Never reproduce against another
person's host or protected Docker workloads.

The engine executes repository tests on the host. It is not a sandbox for hostile
code. See [architecture](docs/architecture.md) for the supported trust boundary.
No security support SLA is promised for this early release.
