---
name: integration-test-verification
description: Verify shipped integration paths and separate synthetic evidence from live acceptance.
license: MIT
metadata:
  hermes:
    category: software-factory
---

# Integration Test Verification

Identify the entry point users or agents actually call, the transport, services,
and state needed to exercise it. Test through that entry point, including invalid
inputs, authorization boundaries, stale state, retries, and failure recovery when
relevant to the contract.

Use disposable fixtures and the sanctioned factory runner for Compose workloads.
Do not substitute helper tests for an untested transport or deployed path. Read
actual result artifacts and record the revision, command, environment inputs,
observed result, and remaining gaps without recording secret values.

If a service or credential is unavailable, finish independent checks and report
that acceptance dimension as INCONCLUSIVE. Never redirect tests toward production
or another user's resources to obtain a green result.
