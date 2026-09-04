#!/usr/bin/env sh
set -eu

if [ "$#" -ne 0 ]; then
    echo "Usage: $0" >&2
    exit 2
fi

repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
version=$(sed -n '1p' "$repo_dir/VERSION")
tag="v$version"
archive_name="codexbar-gnome-$version.tar.gz"
checksum_name="$archive_name.sha256"
download_dir=$(mktemp -d)
trap 'rm -rf -- "$download_dir"' EXIT HUP INT TERM

repository=$(cd "$repo_dir" && gh repo view --json nameWithOwner --jq .nameWithOwner)
release_contract=$(python3 - \
    "$repo_dir" "$repository" "$repo_dir/.release/evidence/q04b-github-release.json" \
    <<'PY'
import json
import re
import subprocess
import sys
from pathlib import Path

repo_dir, repository, receipt_name = sys.argv[1:]
origin_result = subprocess.run(
    ["git", "-C", repo_dir, "remote", "get-url", "origin"],
    capture_output=True,
    text=True,
    check=False,
)
if origin_result.returncode != 0:
    raise SystemExit("verify-release: cannot inspect git origin")
origin = origin_result.stdout.strip()
matched = None
for pattern in (
    r"https?://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$",
    r"git@github\.com:([^/]+/[^/]+?)(?:\.git)?$",
    r"ssh://git@github\.com/([^/]+/[^/]+?)(?:\.git)?/?$",
):
    found = re.fullmatch(pattern, origin, flags=re.IGNORECASE)
    if found is not None:
        matched = found.group(1)
        break
if matched is None or matched.casefold() != repository.casefold():
    raise SystemExit("verify-release: GitHub repository does not match origin")
receipt = json.loads(Path(receipt_name).read_text(encoding="utf-8"))
if (
    receipt.get("schemaVersion") != 1
    or receipt.get("bddId") != "BDD-Q04B"
    or receipt.get("status") != "passed"
    or re.fullmatch(r"[0-9a-f]{40,64}", str(receipt.get("commitSha"))) is None
    or re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("archiveSha256"))) is None
    or set(receipt) != {"schemaVersion", "bddId", "status", "commitSha", "archiveSha256"}
):
    raise SystemExit("verify-release: Q04B receipt is invalid")
print(receipt["commitSha"], receipt["archiveSha256"])
PY
)
release_commit=${release_contract%% *}
expected_digest=${release_contract#* }
metadata=$(gh -R "$repository" release view "$tag" --json tagName,isDraft,targetCommitish,assets)
python3 - "$metadata" "$tag" "$release_commit" "$archive_name" "$checksum_name" "$expected_digest" <<'PY'
import hashlib
import json
import re
import sys

value = json.loads(sys.argv[1])
if value.get("tagName") != sys.argv[2] or value.get("isDraft") is not False or value.get("targetCommitish") != sys.argv[3]:
    raise SystemExit("verify-release: published release metadata does not match Q04B")
assets = value.get("assets")
if (
    not isinstance(assets, list)
    or len(assets) != 2
    or any(not isinstance(item, dict) for item in assets)
    or {item.get("name") for item in assets} != {sys.argv[4], sys.argv[5]}
):
    raise SystemExit("verify-release: published release assets do not match")
expected_digests = {
    sys.argv[4]: "sha256:" + sys.argv[6],
    sys.argv[5]: "sha256:"
    + hashlib.sha256(f"{sys.argv[6]}  {sys.argv[4]}\n".encode("ascii")).hexdigest(),
}
for asset in assets:
    size = asset.get("size")
    digest = asset.get("digest")
    if type(size) is not int or size < 0:
        raise SystemExit("verify-release: published release asset size is invalid")
    if digest is not None and (
        not isinstance(digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        or digest != expected_digests[asset["name"]]
    ):
        raise SystemExit("verify-release: published release asset digest does not match Q04B")
PY

gh -R "$repository" release download "$tag" \
    --dir "$download_dir" \
    --pattern "$archive_name" \
    --pattern "$checksum_name"

archive_digest=$(python3 - "$download_dir/$archive_name" "$download_dir/$checksum_name" "$expected_digest" "$metadata" <<'PY'
import hashlib
import json
import re
import stat
import sys
from pathlib import Path

archive = Path(sys.argv[1])
checksum = Path(sys.argv[2])
assets = {item["name"]: item for item in json.loads(sys.argv[4])["assets"]}
for path in (archive, checksum):
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode) or path.is_symlink():
        raise SystemExit(f"verify-release: missing or unsafe asset: {path.name}")
    if path.stat().st_size != assets[path.name]["size"]:
        raise SystemExit(f"verify-release: downloaded asset size mismatch: {path.name}")
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
if digest != sys.argv[3]:
    raise SystemExit("verify-release: downloaded archive does not match Q04B")
expected = f"{digest}  {archive.name}\n"
payload = checksum.read_text(encoding="ascii")
if re.fullmatch(r"[0-9a-f]{64}  [A-Za-z0-9._-]+\n", payload) is None or payload != expected:
    raise SystemExit("verify-release: downloaded checksum does not match archive")
print(digest)
PY
)

e2e_home="$download_dir/home"
e2e_runtime="$download_dir/runtime"
mkdir -p "$e2e_home" "$e2e_runtime"
chmod 0700 "$e2e_home" "$e2e_runtime"
HOME="$e2e_home" \
XDG_CONFIG_HOME="$e2e_home/.config" \
XDG_DATA_HOME="$e2e_home/.local/share" \
XDG_STATE_HOME="$e2e_home/.local/state" \
XDG_RUNTIME_DIR="$e2e_runtime" \
dbus-run-session -- xvfb-run -a python3 "$repo_dir/scripts/e2e-x11.py" \
    --archive "$download_dir/$archive_name" \
    --report "$download_dir/e2e-x11.json" \
    --probe-dir "$download_dir/probe"

python3 - "$download_dir/e2e-x11.json" \
    "$repo_dir/.release/evidence/q05-host-smoke.json" \
    "$release_commit" "$archive_digest" <<'PY'
import json
import os
import tempfile
import sys
from pathlib import Path

e2e = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if (
    e2e.get("schemaVersion") != 1
    or e2e.get("status") != "passed"
    or e2e.get("archiveSha256") != sys.argv[4]
    or e2e.get("skipped") != []
    or e2e.get("installedHashesMatchedArchive") is not True
    or e2e.get("uninstallClean") is not True
):
    raise SystemExit("verify-release: downloaded artifact smoke did not pass")
receipt = Path(sys.argv[2])
receipt.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
value = {
    "schemaVersion": 1,
    "bddId": "BDD-Q05",
    "status": "passed",
    "commitSha": sys.argv[3],
    "archiveSha256": sys.argv[4],
}
descriptor, temporary_name = tempfile.mkstemp(prefix=f".{receipt.name}.", dir=receipt.parent)
temporary = Path(temporary_name)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, receipt)
finally:
    if temporary.exists():
        temporary.unlink()
PY

echo "Downloaded release $tag passed exact-artifact health verification"
