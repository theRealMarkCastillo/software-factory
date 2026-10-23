---
name: one-verifiable-change
description: Keep a factory implementation increment bounded and independently verifiable.
license: MIT
metadata:
  hermes:
    category: software-factory
---

# One Verifiable Change

Choose one outcome with explicit acceptance criteria and a narrow diff. Separate
unrelated repairs unless the operator expands scope. Keep the implementation,
regression tests, and necessary documentation together.

Verify the outcome through the shipped path, inspect the final diff, and report
which checks actually ran. Do not use a broad refactor to hide an unverified fix.
Preserve unrelated working-tree changes. Commit only when authorized and stage
explicit paths.
