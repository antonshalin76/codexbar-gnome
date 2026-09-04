#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

report_path="$repo_root/.release/evidence/quality-gate.json"
report_explicit=0
preflight_only=0
while (($# > 0)); do
    case "$1" in
        --report)
            (($# >= 2)) || {
                echo "quality-gate: --report requires a value" >&2
                exit 2
            }
            report_path=$2
            report_explicit=1
            shift 2
            ;;
        --preflight-only)
            preflight_only=1
            shift
            ;;
        *)
            echo "quality-gate: unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

if ((preflight_only == 1 && report_explicit == 0)); then
    report_path="$repo_root/.release/evidence/quality-gate-preflight.json"
fi

mkdir -p "$(dirname -- "$report_path")"
command_log=$(mktemp)
test_report=$(mktemp)
release_commit=$(git rev-parse HEAD 2>/dev/null || true)
trap 'rm -f "$command_log" "$test_report"' EXIT

render_report() {
    local status=$1
    local missing=${2-}
    python3 - "$report_path" "$command_log" "$test_report" "$status" "$missing" "$release_commit" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

report_path = Path(sys.argv[1])
command_log = Path(sys.argv[2])
test_report_path = Path(sys.argv[3])
status = sys.argv[4]
missing = [item for item in sys.argv[5].split(":") if item]
release_commit = sys.argv[6]
commands = []
for line in command_log.read_text(encoding="utf-8").splitlines():
    name, command_status = line.split("\t", 1)
    commands.append({"name": name, "status": command_status})
test_summary = None
if test_report_path.is_file() and test_report_path.stat().st_size:
    try:
        test_value = json.loads(test_report_path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        test_value = None
    if isinstance(test_value, dict):
        test_summary = test_value.get("summary")
bdd_passed = any(
    item["name"] == "bdd-manifest" and item["status"] == "passed" for item in commands
)
value = {
    "schemaVersion": 1,
    "mode": "preflight" if os.environ.get("CODEXBAR_QUALITY_PREFLIGHT") == "1" else "full",
    "status": status,
    "missingTools": missing,
    "commands": commands,
    "commitSha": os.environ.get("GITHUB_SHA") or release_commit,
    "testSummary": test_summary,
    "bddSummary": {
        "status": "passed" if bdd_passed else "pending",
        "missing": 0 if bdd_passed else None,
        "duplicate": 0 if bdd_passed else None,
        "unknown": 0 if bdd_passed else None,
        "skipped": (test_summary or {}).get("skipped"),
    },
}
descriptor, temporary_name = tempfile.mkstemp(
    prefix=f".{report_path.name}.", dir=report_path.parent
)
temporary = Path(temporary_name)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, report_path)
finally:
    if temporary.exists():
        temporary.unlink()
PY
}

run_gate() {
    local name=$1
    shift
    echo "==> $name"
    if "$@"; then
        printf '%s\tpassed\n' "$name" >>"$command_log"
        render_report running ""
        return 0
    fi
    printf '%s\tfailed\n' "$name" >>"$command_log"
    render_report failed ""
    return 1
}

render_report running ""
if ((preflight_only == 0)); then
    run_gate git-diff git diff --check
    run_gate git-diff-staged git diff --cached --check
fi

required_tools=(
    git
    python3
    ruff
    shellcheck
    sha256sum
    desktop-file-validate
    bwrap
    dbus-run-session
    gapplication
    gdbus
    gtk-launch
    xvfb-run
)
missing_tools=()
for tool in "${required_tools[@]}"; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        missing_tools+=("$tool")
    fi
done
if ((${#missing_tools[@]} > 0)); then
    missing_joined=$(IFS=:; echo "${missing_tools[*]}")
    render_report failed "$missing_joined"
    echo "Install required tools before running the quality gate: ${missing_tools[*]}" >&2
    exit 1
fi
if ((preflight_only == 1)); then
    CODEXBAR_QUALITY_PREFLIGHT=1 render_report passed ""
    echo "Quality gate preflight passed"
    exit 0
fi

run_gate python-compile python3 -m py_compile \
    bin/codexbar-gnome-indicator \
    scripts/e2e-x11.py \
    scripts/run-tests.py \
    scripts/validate-bdd-manifest.py
run_gate ruff-check ruff check bin/codexbar-gnome-indicator tests scripts
run_gate ruff-format ruff format --check bin/codexbar-gnome-indicator tests scripts
run_gate shellcheck shellcheck \
    install.sh \
    uninstall.sh \
    scripts/build-release.sh \
    scripts/install-git-hooks.sh \
    scripts/publish-release.sh \
    scripts/quality-gate.sh \
    scripts/verify-release.sh \
    .githooks/pre-commit

run_gate unit-e2e-tests env \
    CODEXBAR_GATE_REPORT="$report_path" \
    dbus-run-session -- xvfb-run -a \
    python3 scripts/run-tests.py --report "$test_report"
run_gate bdd-manifest python3 scripts/validate-bdd-manifest.py \
    --manifest tests/bdd_manifest.json \
    --test-report "$test_report"
run_gate documentation-contract env CODEXBAR_TEST_RUNNER_CHILD=1 \
    python3 -m unittest \
    tests.test_deployment_release.DeploymentReleaseTests.test_bdd_q01b_documentation_matches_runtime_and_release_contract
run_gate release-dry-run scripts/publish-release.sh --dry-run

render_report passed ""
echo "Quality gate passed"
