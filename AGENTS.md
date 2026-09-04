# Repository Instructions

## Quality Gate

Before any commit, push, or GitHub publication from this repository:

1. Run:
   `scripts/quality-gate.sh`
2. Fix issues reported by the gate.
3. Rerun:
   `scripts/quality-gate.sh`
4. Commit and push only after the gate passes.

Do not publish source changes through the GitHub contents API, web editor, or any
other direct-write path unless the same exact local diff has already passed
`scripts/quality-gate.sh` in this checkout and the final answer records that
evidence. ShellCheck and every other tool reported by the gate preflight are
required; missing tools are a blocking failure, not a skipped check.

Release publication requires the pre-publication `BDD-E03`, `BDD-E06`, and
`BDD-Q03` receipts declared in `tests/bdd_manifest.json`, each bound to the exact
commit and archive before `scripts/publish-release.sh` creates a tag or draft.
The script records `BDD-Q04B` only after verified publication; the downloaded
release smoke records `BDD-Q05` afterward.
