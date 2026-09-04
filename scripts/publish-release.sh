#!/usr/bin/env sh
set -eu

repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
exec python3 - "$repo_dir" "$@" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path


class PublishError(RuntimeError):
    pass


repository = Path(sys.argv[1]).resolve()
arguments = sys.argv[2:]
dry_run = False
archive: Path | None = None
checksum: Path | None = None
index = 0
while index < len(arguments):
    argument = arguments[index]
    if argument == "--dry-run":
        dry_run = True
        index += 1
    elif argument in {"--archive", "--checksum"}:
        if index + 1 >= len(arguments):
            raise SystemExit(f"publish-release: {argument} requires a value")
        value = Path(arguments[index + 1]).resolve()
        if argument == "--archive":
            archive = value
        else:
            checksum = value
        index += 2
    else:
        raise SystemExit(f"publish-release: unknown argument: {argument}")


def run(
    command: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "command failed"
        detail = re.sub(
            r"(?i)(?:sk-|ghp_|github_pat_)[A-Za-z0-9_]{20,}",
            "[redacted]",
            detail,
        )
        detail = re.sub(r"(https?://)[^/@\s:]+:[^/@\s]+@", r"\1[redacted]@", detail)
        raise PublishError(f"{command[0]} failed: {detail[:512]}")
    return completed


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["git", *args], check=check)


def gh(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["gh", *args], check=check)


def regular(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(metadata.st_mode) and not path.is_symlink()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


version_payload = (repository / "VERSION").read_bytes()
try:
    version = version_payload.decode("ascii").strip()
except UnicodeDecodeError as error:
    raise SystemExit(f"publish-release: invalid VERSION: {error}")
if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
    raise SystemExit("publish-release: VERSION must be a semantic version")
if version_payload != f"{version}\n".encode("ascii"):
    raise SystemExit("publish-release: VERSION must end with one newline")

tag = f"v{version}"
archive_name = f"codexbar-gnome-{version}.tar.gz"
checksum_name = f"{archive_name}.sha256"
if (archive is None) != (checksum is None):
    raise SystemExit("publish-release: --archive and --checksum must be supplied together")
if not dry_run and (archive is None or checksum is None):
    raise SystemExit("publish-release: --archive and --checksum are required")

asset_contract: dict[str, tuple[int, str]] = {}
archive_digest = ""
if archive is not None and checksum is not None:
    if archive.name != archive_name or checksum.name != checksum_name:
        raise SystemExit("publish-release: artifact names do not match VERSION")
    if not regular(archive) or not regular(checksum):
        raise SystemExit("publish-release: artifacts must be regular files")
    archive_size = archive.stat().st_size
    archive_digest = sha256(archive)
    expected_checksum = f"{archive_digest}  {archive_name}\n".encode("ascii")
    if checksum.stat().st_size != len(expected_checksum) or checksum.read_bytes() != expected_checksum:
        raise SystemExit("publish-release: checksum does not match the archive")
    asset_contract = {
        archive_name: (archive_size, f"sha256:{archive_digest}"),
        checksum_name: (
            len(expected_checksum),
            f"sha256:{hashlib.sha256(expected_checksum).hexdigest()}",
        ),
    }

head = git("rev-parse", "HEAD").stdout.strip()
if not re.fullmatch(r"[0-9a-f]{40,64}", head):
    raise SystemExit("publish-release: cannot resolve the release commit")


def validate_archive_matches_head() -> None:
    assert archive is not None
    root = f"codexbar-gnome-{version}"
    committed_sources = {
        f"{root}/VERSION": "VERSION",
        f"{root}/CHANGELOG.md": "CHANGELOG.md",
        f"{root}/LICENSE": "LICENSE",
        f"{root}/README.md": "README.md",
        f"{root}/install.sh": "install.sh",
        f"{root}/uninstall.sh": "uninstall.sh",
        f"{root}/bin/codexbar-gnome-indicator": "bin/codexbar-gnome-indicator",
        f"{root}/share/codexbar-gnome-indicator.desktop": "share/codexbar-gnome-indicator.desktop",
    }
    expected_names = {
        root,
        f"{root}/bin",
        f"{root}/share",
        *committed_sources,
    }
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            members = bundle.getmembers()
            if {member.name for member in members} != expected_names:
                raise PublishError("release archive members do not match HEAD")
            for member in members:
                if member.name in committed_sources:
                    if not member.isfile():
                        raise PublishError("release archive members do not match HEAD")
                    source = bundle.extractfile(member)
                    if source is None:
                        raise PublishError("release archive members do not match HEAD")
                    committed = subprocess.run(
                        ["git", "show", f"{head}:{committed_sources[member.name]}"],
                        cwd=repository,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                    if committed.returncode != 0 or source.read() != committed.stdout:
                        raise PublishError("release archive payload does not match HEAD")
                elif not member.isdir():
                    raise PublishError("release archive members do not match HEAD")
    except (OSError, tarfile.TarError) as error:
        raise PublishError("release archive is invalid") from error


if archive is not None:
    validate_archive_matches_head()


def local_tag() -> str | None:
    listed = git("tag", "--list", tag).stdout.strip()
    return listed or None


def remote_refs() -> dict[str, str]:
    result = git(
        "ls-remote",
        "--tags",
        "origin",
        f"refs/tags/{tag}",
        f"refs/tags/{tag}^{{}}",
        check=False,
    )
    if result.returncode != 0:
        raise PublishError("cannot inspect the remote release tag")
    refs: dict[str, str] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if (
            len(fields) != 2
            or re.fullmatch(r"[0-9a-f]{40,64}", fields[0]) is None
            or fields[1]
            not in {f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"}
            or fields[1] in refs
        ):
            raise PublishError("remote release tag metadata is invalid")
        refs[fields[1]] = fields[0]
    return refs


def validate_existing_tag(remote: dict[str, str]) -> None:
    if local_tag() is None:
        if remote:
            raise PublishError("remote release tag exists without a matching local tag")
        return
    if git("cat-file", "-t", tag).stdout.strip() != "tag":
        raise PublishError("release tag is not annotated")
    local_object = git("rev-parse", f"{tag}^{{tag}}").stdout.strip()
    local_target = git("rev-parse", f"{tag}^{{}}").stdout.strip()
    if local_target != head:
        raise PublishError("release tag targets a different commit")
    remote_object = remote.get(f"refs/tags/{tag}")
    remote_target = remote.get(f"refs/tags/{tag}^{{}}")
    if remote_object is None or remote_target is None:
        raise PublishError("local and remote release tags are not the same annotated tag")
    if remote_object != local_object or remote_target != head:
        raise PublishError("local and remote release tags do not match")


def validate_github_repository() -> str:
    origin = git("remote", "get-url", "origin").stdout.strip()
    expected: str | None = None
    for pattern in (
        r"https?://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$",
        r"git@github\.com:([^/]+/[^/]+?)(?:\.git)?$",
        r"ssh://git@github\.com/([^/]+/[^/]+?)(?:\.git)?/?$",
    ):
        matched = re.fullmatch(pattern, origin, flags=re.IGNORECASE)
        if matched is not None:
            expected = matched.group(1)
            break
    if "github.com" in origin.lower() and expected is None:
        raise PublishError("cannot identify the GitHub origin repository")

    viewed = gh("repo", "view", "--json", "nameWithOwner")
    try:
        value = json.loads(viewed.stdout)
    except json.JSONDecodeError as error:
        raise PublishError("GitHub returned invalid repository metadata") from error
    actual = value.get("nameWithOwner") if isinstance(value, dict) else None
    if (
        not isinstance(actual, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", actual) is None
    ):
        raise PublishError("GitHub returned invalid repository metadata")
    if expected is not None and actual.casefold() != expected.casefold():
        raise PublishError("GitHub CLI repository does not match git origin")
    return actual.casefold()


def validate_release_branch() -> None:
    result = git("ls-remote", "--heads", "origin", "refs/heads/master", check=False)
    fields = result.stdout.split()
    if (
        result.returncode != 0
        or len(fields) != 2
        or fields[1] != "refs/heads/master"
        or fields[0] != head
    ):
        raise PublishError("origin/master does not match the release commit")


if dry_run:
    if local_tag() is not None:
        if git("cat-file", "-t", tag).stdout.strip() != "tag":
            raise SystemExit("publish-release: release tag is not annotated")
        if git("rev-parse", f"{tag}^{{}}").stdout.strip() != head:
            raise SystemExit("publish-release: release tag targets a different commit")
    print(f"Dry run: publish {tag} from {head}")
    print(f"Archive: {archive_name}")
    print(f"Checksum: {checksum_name}")
    raise SystemExit(0)

if git("status", "--porcelain", "--untracked-files=all").stdout:
    raise SystemExit("publish-release: refusing to publish a dirty checkout")


def load_unique_json(path: Path) -> object:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise PublishError(f"duplicate key in release evidence: {key}")
            value[key] = item
        return value

    if not regular(path) or path.stat().st_size > 64 * 1024:
        raise PublishError(f"missing or unsafe release receipt: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PublishError(f"invalid release evidence: {path}") from error


def external_scenarios() -> dict[str, object]:
    manifest_value = load_unique_json(repository / "tests" / "bdd_manifest.json")
    if not isinstance(manifest_value, dict):
        raise PublishError("invalid BDD manifest for release evidence")
    scenarios = manifest_value.get("scenarios")
    if not isinstance(scenarios, dict):
        raise PublishError("invalid BDD manifest for release evidence")
    return scenarios


def external_receipt_path(scenarios: dict[str, object], scenario_id: str) -> Path:
    release_root = repository / ".release"
    raw_evidence_root = release_root / "evidence"
    if release_root.is_symlink() or raw_evidence_root.is_symlink():
        raise PublishError("unsafe release evidence directory")
    evidence_root = raw_evidence_root.resolve()
    entry = scenarios.get(scenario_id)
    if not isinstance(entry, dict) or entry.get("status") != "external-gate":
        raise PublishError(f"missing release evidence mapping for {scenario_id}")
    receipt_name = entry.get("receipt")
    if (
        not isinstance(receipt_name, str)
        or re.fullmatch(r"\.release/evidence/[A-Za-z0-9._-]+\.json", receipt_name)
        is None
    ):
        raise PublishError(f"missing release receipt for {scenario_id}")
    raw_receipt = repository / receipt_name
    if raw_receipt.is_symlink():
        raise PublishError(f"unsafe release receipt path for {scenario_id}")
    receipt = raw_receipt.resolve()
    if receipt.parent != evidence_root:
        raise PublishError(f"unsafe release receipt path for {scenario_id}")
    return receipt


def expected_receipt(scenario_id: str) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "bddId": scenario_id,
        "status": "passed",
        "commitSha": head,
        "archiveSha256": archive_digest,
    }


scenarios = external_scenarios()


def validate_external_evidence(required_ids: set[str]) -> None:
    for scenario_id in sorted(required_ids):
        receipt = external_receipt_path(scenarios, scenario_id)
        value = load_unique_json(receipt)
        if value != expected_receipt(scenario_id):
            raise PublishError(f"release receipt is not bound to this build: {scenario_id}")


def write_external_evidence(scenario_id: str) -> None:
    receipt = external_receipt_path(scenarios, scenario_id)
    receipt.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{receipt.name}.", dir=receipt.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as stream:
            json.dump(expected_receipt(scenario_id), stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, receipt)
        directory = os.open(receipt.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


publication_marker = repository / ".release" / "evidence" / "publication-transaction.json"
github_repository: str | None = None


def expected_publication_marker() -> dict[str, object]:
    if github_repository is None:
        raise PublishError("GitHub repository identity is unavailable")
    return {
        "schemaVersion": 1,
        "repository": github_repository,
        "tag": tag,
        "commitSha": head,
        "archiveSha256": archive_digest,
    }


def read_publication_marker() -> dict[str, object] | None:
    if not publication_marker.exists() and not publication_marker.is_symlink():
        return None
    value = load_unique_json(publication_marker)
    if not isinstance(value, dict) or value != expected_publication_marker():
        raise PublishError("publication transaction marker does not match this release")
    return value


def write_publication_marker() -> None:
    if publication_marker.is_symlink():
        raise PublishError("unsafe publication transaction marker")
    publication_marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{publication_marker.name}.", dir=publication_marker.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as stream:
            json.dump(expected_publication_marker(), stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, publication_marker)
        directory = os.open(
            publication_marker.parent, os.O_RDONLY | os.O_DIRECTORY
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def remove_publication_marker() -> None:
    if not publication_marker.exists() and not publication_marker.is_symlink():
        return
    if publication_marker.is_symlink() or not regular(publication_marker):
        raise PublishError("unsafe publication transaction marker")
    publication_marker.unlink()
    directory = os.open(publication_marker.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


try:
    validate_external_evidence({"BDD-E03", "BDD-E06", "BDD-Q03"})
except (OSError, PublishError) as error:
    print(f"publish-release: evidence preflight failed: {error}", file=sys.stderr)
    raise SystemExit(1) from None


def release_view() -> dict[str, object] | None:
    viewed = gh(
        "release",
        "view",
        tag,
        "--json",
        "tagName,isDraft,targetCommitish,assets,url",
        check=False,
    )
    if viewed.returncode != 0:
        detail = viewed.stderr.strip() or viewed.stdout.strip()
        if detail and re.search(r"(?i)(?:release )?not found|HTTP 404", detail) is None:
            raise PublishError("cannot inspect the GitHub release")
        return None
    try:
        value = json.loads(viewed.stdout)
    except json.JSONDecodeError as error:
        raise PublishError("GitHub returned invalid release metadata") from error
    if not isinstance(value, dict):
        raise PublishError("GitHub returned invalid release metadata")
    return value


def validate_release(value: dict[str, object], *, draft: bool) -> None:
    if value.get("tagName") != tag or value.get("isDraft") is not draft:
        raise PublishError("existing GitHub release state does not match")
    if value.get("targetCommitish") != head:
        raise PublishError("GitHub release targets a different commit")
    assets = value.get("assets")
    if not isinstance(assets, list) or len(assets) != len(asset_contract):
        raise PublishError("GitHub release assets do not match")
    observed: dict[str, int] = {}
    for item in assets:
        if not isinstance(item, dict):
            raise PublishError("GitHub release assets do not match")
        name = item.get("name")
        size = item.get("size")
        digest = item.get("digest")
        if (
            not isinstance(name, str)
            or type(size) is not int
            or size < 0
            or (
                digest is not None
                and (
                    not isinstance(digest, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
                )
            )
        ):
            raise PublishError("GitHub release assets do not match")
        if name in observed:
            raise PublishError("GitHub release contains duplicate assets")
        expected = asset_contract.get(name)
        if expected is None or size != expected[0]:
            raise PublishError("GitHub release asset digest or size does not match")
        if digest is not None and digest != expected[1]:
            raise PublishError("GitHub release asset digest or size does not match")
        observed[name] = size
    if set(observed) != set(asset_contract):
        raise PublishError("GitHub release asset digest or size does not match")


def verify_release_asset_bytes() -> None:
    with tempfile.TemporaryDirectory(prefix="codexbar-release-readback-") as raw_dir:
        directory = Path(raw_dir)
        downloaded = gh("release", "download", tag, "--dir", str(directory), check=False)
        if downloaded.returncode != 0:
            raise PublishError("GitHub release assets are not ready for download")
        entries = list(directory.iterdir())
        if {entry.name for entry in entries} != set(asset_contract):
            raise PublishError("GitHub release downloaded assets do not match")
        for entry in entries:
            expected_size, expected_digest = asset_contract[entry.name]
            if not regular(entry):
                raise PublishError("GitHub release downloaded asset is unsafe")
            if entry.stat().st_size != expected_size:
                raise PublishError("GitHub release downloaded asset size does not match")
            if f"sha256:{sha256(entry)}" != expected_digest:
                raise PublishError("GitHub release downloaded asset digest does not match")


def wait_for_owned_release_assets(*, draft: bool) -> dict[str, object]:
    deadline = time.monotonic() + 30
    while True:
        value = release_view()
        if value is None:
            raise PublishError("owned GitHub release disappeared during verification")
        try:
            validate_release(value, draft=draft)
            verify_release_asset_bytes()
            return value
        except PublishError as error:
            if not str(error).startswith("GitHub release asset"):
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


resume_draft = False
created_local_tag = False
created_remote_tag = False
created_release = False
published = False

try:
    github_repository = validate_github_repository()
    remote = remote_refs()
    validate_existing_tag(remote)
    existing_release = release_view()
    if existing_release is not None:
        if existing_release.get("isDraft") is True:
            validate_release(existing_release, draft=True)
            verify_release_asset_bytes()
            if read_publication_marker() is None:
                raise PublishError("pre-existing draft is not owned by this transaction")
            resume_draft = True
            created_local_tag = True
            created_remote_tag = True
            created_release = True
        else:
            validate_release(existing_release, draft=False)
            verify_release_asset_bytes()
            if read_publication_marker() is not None:
                remove_publication_marker()
            write_external_evidence("BDD-Q04B")
            print(f"Release {tag} is already published and verified")
            raise SystemExit(0)
    if not resume_draft and (local_tag() is not None or remote):
        raise PublishError("pre-existing tag has no matching release")
    if not resume_draft:
        if read_publication_marker() is not None:
            raise PublishError("stale publication transaction marker")
        validate_release_branch()
except (OSError, PublishError) as error:
    print(f"publish-release: preflight failed: {error}", file=sys.stderr)
    raise SystemExit(1) from None

def fail_at(name: str) -> None:
    if os.environ.get("CODEXBAR_PUBLISH_TEST_FAIL_PHASE") == name:
        raise PublishError(f"injected publication failure at {name}")


def rollback() -> list[str]:
    failures: list[str] = []
    if created_release:
        deleted = gh("release", "delete", tag, "--yes", check=False)
        if deleted.returncode != 0:
            failures.append("draft release")
            return failures
    if created_remote_tag:
        deleted = git("push", "origin", f":refs/tags/{tag}", check=False)
        if deleted.returncode != 0:
            failures.append("remote tag")
            return failures
    if created_local_tag:
        deleted = git("tag", "--delete", tag, check=False)
        if deleted.returncode != 0:
            failures.append("local tag")
            return failures
    try:
        remove_publication_marker()
    except (OSError, PublishError):
        failures.append("publication marker")
    return failures


try:
    if not resume_draft:
        created_tag = git(
            "-c",
            "user.name=codexbar-gnome release",
            "-c",
            "user.email=noreply@codexbar-gnome.invalid",
            "tag",
            "--annotate",
            tag,
            "--message",
            f"codexbar-gnome {version}",
            head,
            check=False,
        )
        if created_tag.returncode != 0:
            if (
                local_tag() is not None
                and git("cat-file", "-t", tag).stdout.strip() == "tag"
                and git("rev-parse", f"{tag}^{{}}").stdout.strip() == head
            ):
                created_local_tag = True
            raise PublishError("cannot create the annotated release tag")
        created_local_tag = True
        pushed_tag = git("push", "origin", f"refs/tags/{tag}", check=False)
        if pushed_tag.returncode != 0:
            observed_remote = remote_refs()
            if (
                observed_remote.get(f"refs/tags/{tag}")
                == git("rev-parse", f"{tag}^{{tag}}").stdout.strip()
                and observed_remote.get(f"refs/tags/{tag}^{{}}") == head
            ):
                created_remote_tag = True
            raise PublishError("cannot push the annotated release tag")
        created_remote_tag = True
        fail_at("tag-created")

        created = gh(
            "release",
            "create",
            tag,
            "--draft",
            "--target",
            head,
            "--title",
            f"codexbar-gnome {version}",
            "--notes-file",
            str(repository / "CHANGELOG.md"),
            check=False,
        )
        if created.returncode != 0:
            observed = release_view()
            if (
                observed is not None
                and observed.get("tagName") == tag
                and observed.get("isDraft") is True
                and observed.get("targetCommitish") == head
                and observed.get("assets") == []
            ):
                created_release = True
            raise PublishError("cannot create the draft GitHub release")
        created_release = True
        fail_at("draft-created")

        assert archive is not None and checksum is not None
        gh("release", "upload", tag, str(archive), "--clobber")
        fail_at("archive-uploaded")
        gh("release", "upload", tag, str(checksum), "--clobber")
        fail_at("checksum-uploaded")

        wait_for_owned_release_assets(draft=True)
        fail_at("verified")

    write_publication_marker()

    edited = gh("release", "edit", tag, "--draft=false", check=False)
    try:
        published_release = release_view()
    except PublishError:
        published = True
        raise PublishError("publication result is ambiguous; retry to reconcile") from None
    if published_release is None:
        published = True
        raise PublishError("publication result is ambiguous; retry to reconcile")
    draft_state = published_release.get("isDraft")
    if draft_state is True:
        raise PublishError("cannot publish the draft GitHub release")
    if draft_state is not False:
        published = True
        raise PublishError("publication result is ambiguous; retry to reconcile")
    published = True
    validate_release(published_release, draft=False)
    verify_release_asset_bytes()
    fail_at("published")
    write_external_evidence("BDD-Q04B")
    remove_publication_marker()
except BaseException as error:
    if not published:
        rollback_failures = rollback()
        if rollback_failures:
            print(
                "publish-release: rollback incomplete: "
                + ", ".join(rollback_failures),
                file=sys.stderr,
            )
    if isinstance(error, (PublishError, OSError)):
        print(f"publish-release: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    raise

print(str(published_release.get("url", tag)))
PY
