#!/usr/bin/env sh
set -eu

if [ "$#" -ne 0 ]; then
    echo "Usage: $0" >&2
    exit 2
fi

repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)

# The Python process replaces this shell, so it receives lifecycle signals directly.
trap 'exit 130' INT TERM
exec python3 - "$repo_dir" <<'PY'
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import sys
import tempfile
import time
from pathlib import Path

APP_NAME = "codexbar-gnome"
DESKTOP_NAME = "codexbar-gnome-indicator.desktop"
MANIFEST_NAME = "install-manifest.json"
JOURNAL_NAME = "install-transaction.json"
TRANSACTION_DIR_NAME = ".install-transaction-files"
MANIFEST_KEYS = ("indicator", "desktop", "autostart")


class InstallInterrupted(Exception):
    pass


def _interrupt(_signum: int, _frame: object) -> None:
    raise InstallInterrupted("installation interrupted")


def _directory(path: Path, mode: int, *, enforce_mode: bool = False) -> None:
    existed = path.exists() or path.is_symlink()
    path.mkdir(parents=True, exist_ok=True)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise RuntimeError(f"unsafe directory: {path}")
    if enforce_mode or not existed:
        path.chmod(mode)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_bytes(path: Path, payload: bytes, mode: int) -> None:
    _directory(path.parent, 0o700 if path.parent.name == APP_NAME else 0o755)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_json(path: Path, value: object) -> None:
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    _write_bytes(path, payload, 0o600)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(metadata.st_mode) and not path.is_symlink()


def _remove_tree(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _checkpoint(name: str) -> None:
    if os.environ.get("CODEXBAR_INSTALL_TEST_BLOCK_PHASE") != name:
        return
    ready_name = os.environ.get("CODEXBAR_INSTALL_TEST_READY")
    release_name = os.environ.get("CODEXBAR_INSTALL_TEST_RELEASE")
    if not ready_name or not release_name:
        raise RuntimeError("fault checkpoint paths are incomplete")
    ready = Path(ready_name)
    release = Path(release_name)
    _directory(ready.parent, 0o755)
    ready.touch()
    while not release.exists():
        time.sleep(0.02)


def _fail_at(name: str) -> None:
    if os.environ.get("CODEXBAR_INSTALL_TEST_FAIL_PHASE") == name:
        raise RuntimeError(f"injected installer failure at {name}")


def _load_journal(path: Path) -> dict[str, object]:
    if not _regular_file(path) or path.stat().st_size > 64 * 1024:
        raise RuntimeError("cannot recover installation transaction")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RuntimeError("cannot recover installation transaction")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise RuntimeError("cannot recover installation transaction")
            value[key] = item
        return value

    value = json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=unique_object
    )
    if (
        not isinstance(value, dict)
        or set(value) != {"schemaVersion", "phase", "prior"}
        or value.get("schemaVersion") != 1
    ):
        raise RuntimeError("cannot recover installation transaction")
    if value.get("phase") not in {"prepared", "committed"}:
        raise RuntimeError("cannot recover installation transaction")
    prior = value.get("prior")
    if not isinstance(prior, dict) or set(prior) != {
        *MANIFEST_KEYS,
        "manifest",
    }:
        raise RuntimeError("cannot recover installation transaction")
    return value


def _restore_prior(
    journal: dict[str, object], targets: dict[str, Path], transaction_dir: Path
) -> None:
    prior = journal["prior"]
    assert isinstance(prior, dict)
    restore: dict[str, tuple[bytes, int] | None] = {}
    for key in (*MANIFEST_KEYS, "manifest"):
        entry = prior[key]
        if not isinstance(entry, dict) or set(entry) != {
            "present",
            "mode",
            "backup",
            "sha256",
        }:
            raise RuntimeError("cannot recover installation transaction")
        if entry["present"] is False:
            if entry["mode"] != "0000" or entry["backup"] != "" or entry["sha256"] != "":
                raise RuntimeError("cannot recover installation transaction")
            restore[key] = None
            continue
        if entry["present"] is not True:
            raise RuntimeError("cannot recover installation transaction")
        backup_name = entry["backup"]
        mode_text = entry["mode"]
        digest = entry["sha256"]
        if (
            backup_name != f"{key}.backup"
            or not isinstance(mode_text, str)
            or len(mode_text) != 4
            or any(character not in "01234567" for character in mode_text)
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise RuntimeError("cannot recover installation transaction")
        backup = transaction_dir / backup_name
        if not _regular_file(backup):
            raise RuntimeError("cannot recover installation transaction")
        payload = backup.read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise RuntimeError("cannot recover installation transaction")
        restore[key] = (payload, int(mode_text, 8))

    for key in (*MANIFEST_KEYS, "manifest"):
        target = targets[key]
        saved = restore[key]
        if saved is None:
            if target.is_symlink() or target.exists():
                if not target.is_symlink() and not _regular_file(target):
                    raise RuntimeError("cannot recover installation transaction")
                target.unlink()
        else:
            payload, mode = saved
            _write_bytes(target, payload, mode)


def _recover(journal_path: Path, transaction_dir: Path, targets: dict[str, Path]) -> bool:
    if not journal_path.exists() and not journal_path.is_symlink():
        _remove_tree(transaction_dir)
        return False
    journal = _load_journal(journal_path)
    if journal["phase"] == "prepared":
        _restore_prior(journal, targets, transaction_dir)
    journal_path.unlink()
    _sync_directory(journal_path.parent)
    _remove_tree(transaction_dir)
    return True


def _capture_prior(targets: dict[str, Path], transaction_dir: Path) -> dict[str, object]:
    prior: dict[str, object] = {}
    for key in (*MANIFEST_KEYS, "manifest"):
        target = targets[key]
        if not target.exists() and not target.is_symlink():
            prior[key] = {
                "present": False,
                "mode": "0000",
                "backup": "",
                "sha256": "",
            }
            continue
        if not _regular_file(target):
            raise RuntimeError(f"refusing to replace non-file target: {target}")
        mode = stat.S_IMODE(target.stat().st_mode)
        backup_name = f"{key}.backup"
        backup = transaction_dir / backup_name
        payload = target.read_bytes()
        _write_bytes(backup, payload, mode)
        prior[key] = {
            "present": True,
            "mode": f"{mode:04o}",
            "backup": backup_name,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    return prior


def main() -> int:
    repository = Path(sys.argv[1]).resolve()
    home = Path(os.environ.get("HOME", ""))
    if not home.is_absolute():
        raise RuntimeError("HOME must be an absolute path")
    data_home = Path(os.environ.get("XDG_DATA_HOME") or str(home / ".local/share"))
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or str(home / ".config"))
    state_home = Path(os.environ.get("XDG_STATE_HOME") or str(home / ".local/state"))
    for name, value in (
        ("XDG_DATA_HOME", data_home),
        ("XDG_CONFIG_HOME", config_home),
        ("XDG_STATE_HOME", state_home),
    ):
        if not value.is_absolute():
            raise RuntimeError(f"{name} must be an absolute path")

    state_dir = state_home / APP_NAME
    _directory(state_dir, 0o700, enforce_mode=True)
    state_descriptor = os.open(
        state_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    )
    fcntl.flock(state_descriptor, fcntl.LOCK_EX)
    try:
        targets = {
            "indicator": home / ".local/bin/codexbar-gnome-indicator",
            "desktop": data_home / "applications" / DESKTOP_NAME,
            "autostart": config_home / "autostart" / DESKTOP_NAME,
            "manifest": state_dir / MANIFEST_NAME,
        }
        journal_path = state_dir / JOURNAL_NAME
        transaction_dir = state_dir / TRANSACTION_DIR_NAME
        recovered = _recover(journal_path, transaction_dir, targets)
        if recovered:
            _checkpoint("recovery-complete")

        version_payload = (repository / "VERSION").read_bytes()
        try:
            version = version_payload.decode("ascii").strip()
        except UnicodeDecodeError as error:
            raise RuntimeError("VERSION must contain ASCII") from error
        if (
            re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None
            or version_payload != f"{version}\n".encode("ascii")
        ):
            raise RuntimeError("VERSION must contain one canonical version line")
        source_files = {
            "indicator": repository / "bin/codexbar-gnome-indicator",
            "desktop": repository / "share" / DESKTOP_NAME,
            "autostart": repository / "share" / DESKTOP_NAME,
        }
        for source in source_files.values():
            if not _regular_file(source):
                raise RuntimeError(f"missing regular installation source: {source}")

        _remove_tree(transaction_dir)
        _directory(transaction_dir, 0o700)
        staged: dict[str, Path] = {}
        modes = {"indicator": 0o755, "desktop": 0o644, "autostart": 0o644}
        try:
            for key in MANIFEST_KEYS:
                staged[key] = transaction_dir / f"{key}.new"
                _write_bytes(staged[key], source_files[key].read_bytes(), modes[key])
                _fail_at(f"stage-{key}")

            manifest_value = {
                "schemaVersion": 1,
                "version": version,
                "files": {
                    key: {
                        "type": "file",
                        "sha256": _sha256(staged[key]),
                        "mode": f"{modes[key]:04o}",
                    }
                    for key in MANIFEST_KEYS
                },
            }
            staged["manifest"] = transaction_dir / "manifest.new"
            _write_json(staged["manifest"], manifest_value)
            _fail_at("validate-staged")

            prior = _capture_prior(targets, transaction_dir)
            _fail_at("prepare-journal")
            journal = {"schemaVersion": 1, "phase": "prepared", "prior": prior}
            _write_json(journal_path, journal)
            _checkpoint("prepared")

            for key in MANIFEST_KEYS:
                _directory(targets[key].parent, 0o755)
                os.replace(staged[key], targets[key])
                _sync_directory(targets[key].parent)
                _fail_at(f"commit-{key}")
                _checkpoint(f"{key}-committed")

            os.replace(staged["manifest"], targets["manifest"])
            _sync_directory(targets["manifest"].parent)
            _fail_at("commit-manifest")
            journal["phase"] = "committed"
            _write_json(journal_path, journal)
            _checkpoint("committed")
            _remove_tree(transaction_dir)
            journal_path.unlink()
            _sync_directory(state_dir)
        except BaseException:
            if journal_path.exists() or journal_path.is_symlink():
                _recover(journal_path, transaction_dir, targets)
            else:
                _remove_tree(transaction_dir)
            raise
    finally:
        os.close(state_descriptor)
    print("Installed codexbar-gnome-indicator")
    print("Start it now with: gtk-launch codexbar-gnome-indicator")
    return 0


signal.signal(signal.SIGINT, _interrupt)
signal.signal(signal.SIGTERM, _interrupt)
try:
    raise SystemExit(main())
except InstallInterrupted as error:
    print(f"codexbar-gnome: {error}", file=sys.stderr)
    raise SystemExit(130)
except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
    print(f"codexbar-gnome: installation failed: {error}", file=sys.stderr)
    raise SystemExit(1)
PY
