# Minimal holdout example

This is a public example, **not** a confidential suite. Real holdouts must live
outside the candidate repository and builder-visible filesystem/artifacts.
See [the contract and threat model](../../docs/holdouts.md).

Prepare a standalone clean candidate and an external holdout, from the engine
checkout (use an operator-controlled absolute scratch directory):

```sh
WORK=/absolute/operator-scratch/holdout-demo
mkdir -p "$WORK/candidate" "$WORK/private"
cp examples/holdout-minimal/app.py examples/holdout-minimal/visible.py \
  examples/holdout-minimal/test_app.py \
  examples/holdout-minimal/Dockerfile examples/holdout-minimal/compose.yml \
  examples/holdout-minimal/.env.example "$WORK/candidate/"
cp examples/holdout-minimal/holdout-example/holdout.json \
  examples/holdout-minimal/holdout-example/check.py "$WORK/private/"
git -C "$WORK/candidate" init -b main
git -C "$WORK/candidate" add app.py visible.py test_app.py Dockerfile compose.yml .env.example
git -C "$WORK/candidate" commit -m 'test: add doubling demo'
BASE=$(git -C "$WORK/candidate" rev-parse HEAD)
factory run --card holdout-demo --repo "$WORK/candidate" --base "$BASE" \
  --compose compose.yml --holdout "$WORK/private" \
  --artifacts-root "$WORK/artifacts"
```

Expected: visible test passes, then the independent fresh project passes all
three holdout cases. The factory's host interpreter needs its normal `pytest`
and `pytest-cov` tooling (provided by the engine's locked environment); the
coverage gate runs `test_app.py` there, not inside Docker. No image dependency
installation is needed; Docker must be able to obtain `python:3.12-slim`.
Both app/test services run on the private Compose network, with no published
ports. The initial self-baseline run has no Python delta; the overfit run below
actually exercises coverage of the changed line, without resetting `BASE`.
Coverage measures execution, not correctness: `test_app.py` deliberately tests
only `n=1`, so that line is covered even when it returns the wrong value for
other inputs. Only the external holdout catches those cases.

For the **overfit** variant, change `body = str(number * 2).encode()` in `app.py`
to `body = b"2"`, then commit that exact file. Run the same command with a new
card ID. The visible `n=1` test still passes; holdout `n=2` and `n=-3` fail. The
run exits nonzero with stage `holdout_failed`; private diagnostics remain under
`$WORK/private/runs/`, while the public manifest contains digests/counts only.

To verify a passing run, use its exact generated artifacts directory:

```sh
CANDIDATE=$(git -C "$WORK/candidate" rev-parse HEAD)
factory verify --repo "$WORK/candidate" --base "$BASE" --candidate "$CANDIDATE" \
  --artifacts-dir "$ARTIFACTS" --holdout "$WORK/private" --json
```

Changing `check.py` after the run makes this verification reject with
`holdout_digest_mismatch`. Without `--holdout`, verification reports the suite
as recorded but not re-verified. These are reproduction instructions, not a
claim that a particular host has completed live Compose acceptance.
