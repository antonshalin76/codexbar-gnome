# Repository Instructions

## SonarQube Gate

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
evidence.
