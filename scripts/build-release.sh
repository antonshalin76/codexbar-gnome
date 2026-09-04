#!/usr/bin/env sh
set -eu

repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
output_dir=

while test "$#" -gt 0; do
    case "$1" in
        --output-dir)
            test "$#" -ge 2 || {
                echo "build-release: --output-dir requires a value" >&2
                exit 2
            }
            output_dir=$2
            shift 2
            ;;
        *)
            echo "build-release: unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

test -n "$output_dir" || {
    echo "build-release: --output-dir is required" >&2
    exit 2
}

if test -n "$(git -C "$repo_dir" status --porcelain --untracked-files=all)"; then
    echo "build-release: refusing to package a dirty checkout" >&2
    exit 1
fi

exec python3 - "$repo_dir" "$output_dir" <<'PY'
from __future__ import annotations

import gzip
import hashlib
import io
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

repository = Path(sys.argv[1]).resolve()
output = Path(sys.argv[2]).resolve()
frozen_head = subprocess.run(
    ["git", "-C", str(repository), "rev-parse", "HEAD"],
    capture_output=True,
    text=True,
    check=True,
).stdout.strip()
ready_name = os.environ.get("CODEXBAR_BUILD_TEST_READY")
release_name = os.environ.get("CODEXBAR_BUILD_TEST_RELEASE")
if ready_name or release_name:
    if not ready_name or not release_name:
        raise SystemExit("build-release: incomplete test checkpoint")
    ready = Path(ready_name)
    release = Path(release_name)
    ready.touch()
    deadline = time.monotonic() + 10
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not release.exists():
        raise SystemExit("build-release: test checkpoint timed out")
version_payload = (repository / "VERSION").read_bytes()
try:
    version = version_payload.decode("ascii").strip()
except UnicodeDecodeError as error:
    raise SystemExit(f"build-release: invalid VERSION: {error}")
if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
    raise SystemExit("build-release: VERSION must be a semantic version")
if version_payload != f"{version}\n".encode("ascii"):
    raise SystemExit("build-release: VERSION must end with one newline")

root = f"codexbar-gnome-{version}"
sources = {
    f"{root}/VERSION": (repository / "VERSION", 0o644),
    f"{root}/CHANGELOG.md": (repository / "CHANGELOG.md", 0o644),
    f"{root}/LICENSE": (repository / "LICENSE", 0o644),
    f"{root}/README.md": (repository / "README.md", 0o644),
    f"{root}/install.sh": (repository / "install.sh", 0o755),
    f"{root}/uninstall.sh": (repository / "uninstall.sh", 0o755),
    f"{root}/bin/codexbar-gnome-indicator": (
        repository / "bin/codexbar-gnome-indicator",
        0o755,
    ),
    f"{root}/share/codexbar-gnome-indicator.desktop": (
        repository / "share/codexbar-gnome-indicator.desktop",
        0o644,
    ),
}
directories = (f"{root}/", f"{root}/bin/", f"{root}/share/")

payloads: dict[str, bytes] = {}
for archive_name, (source, _mode) in sources.items():
    try:
        metadata = source.lstat()
    except FileNotFoundError:
        raise SystemExit(f"build-release: missing source: {source}") from None
    if not stat.S_ISREG(metadata.st_mode) or source.is_symlink():
        raise SystemExit(f"build-release: source is not a regular file: {source}")
    payload = source.read_bytes()
    relative = source.relative_to(repository).as_posix()
    committed = subprocess.run(
        ["git", "-C", str(repository), "show", f"{frozen_head}:{relative}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if committed.returncode != 0 or committed.stdout != payload:
        raise SystemExit(f"build-release: source does not match HEAD: {relative}")
    payloads[archive_name] = payload
current_head = subprocess.run(
    ["git", "-C", str(repository), "rev-parse", "HEAD"],
    capture_output=True,
    text=True,
    check=True,
).stdout.strip()
if current_head != frozen_head:
    raise SystemExit("build-release: HEAD changed while capturing release sources")

combined = b"\n".join(payloads.values())
if b"SECRET-MARKER" in combined or re.search(
    rb"(?:sk-|ghp_|github_pat_)[A-Za-z0-9_]{20,}", combined
):
    raise SystemExit("build-release: release payload contains a secret marker")
try:
    changelog = payloads[f"{root}/CHANGELOG.md"].decode("utf-8")
except UnicodeDecodeError:
    raise SystemExit("build-release: CHANGELOG.md must be UTF-8") from None
if re.search(rf"(?m)^## {re.escape(version)} - \d{{4}}-\d{{2}}-\d{{2}}$", changelog) is None:
    raise SystemExit("build-release: CHANGELOG.md has no entry for VERSION")

output.mkdir(parents=True, exist_ok=True)
if not output.is_dir() or output.is_symlink():
    raise SystemExit("build-release: output path is not a safe directory")
archive_name = f"codexbar-gnome-{version}.tar.gz"
checksum_name = f"{archive_name}.sha256"
archive_path = output / archive_name
checksum_path = output / checksum_name

tar_buffer = io.BytesIO()
with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
    members: dict[str, tuple[str, int]] = {
        name: ("directory", 0o755) for name in directories
    }
    members.update({name: ("file", mode) for name, (_path, mode) in sources.items()})
    for name in sorted(members):
        kind, mode = members[name]
        info = tarfile.TarInfo(name)
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.mtime = 0
        info.mode = mode
        if kind == "directory":
            info.type = tarfile.DIRTYPE
            info.size = 0
            archive.addfile(info)
        else:
            payload = payloads[name]
            info.type = tarfile.REGTYPE
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

descriptor, temporary_name = tempfile.mkstemp(prefix=f".{archive_name}.", dir=output)
temporary = Path(temporary_name)
try:
    with os.fdopen(descriptor, "wb", closefd=True) as raw:
        with gzip.GzipFile(filename="", mode="wb", compresslevel=9, mtime=0, fileobj=raw) as compressed:
            compressed.write(tar_buffer.getvalue())
        raw.flush()
        os.fsync(raw.fileno())
    os.chmod(temporary, 0o644)
    os.replace(temporary, archive_path)
finally:
    if temporary.exists():
        temporary.unlink()

digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
checksum_payload = f"{digest}  {archive_name}\n".encode("ascii")
descriptor, temporary_name = tempfile.mkstemp(prefix=f".{checksum_name}.", dir=output)
temporary = Path(temporary_name)
try:
    os.fchmod(descriptor, 0o644)
    with os.fdopen(descriptor, "wb", closefd=True) as stream:
        stream.write(checksum_payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, checksum_path)
finally:
    if temporary.exists():
        temporary.unlink()

directory_descriptor = os.open(output, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
try:
    os.fsync(directory_descriptor)
finally:
    os.close(directory_descriptor)

print(archive_path)
print(checksum_path)
PY
